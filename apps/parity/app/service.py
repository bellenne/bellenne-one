from collections import defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from .adapters import ApiError, ERRORS, OzonAdapter, WildberriesAdapter
from .domain import NormalizedPrice, PRICE_TYPES, difference, now
from .models import Account, ActiveJob, Comparison, Event, Job, Mapping, Policy, Product, Snapshot, StagedProduct
from .security import decrypt


def write_lock(session):
    if session.bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def log(session, owner, event, job=None, product=None, **details):
    session.add(Event(owner_id=owner, job_id=job, product_id=product, event=event, details=details))


def summarize_errors(results):
    grouped = defaultdict(list)
    labels = {"wb": "Wildberries", "ozon": "Ozon", "collector": "Сборщик"}
    for marketplace, result in results.items():
        message = result.get("message")
        if message:
            grouped[message].append(labels.get(marketplace, marketplace))
    return " ".join(f"{', '.join(markets)}: {message}" for message, markets in grouped.items()) or None


def ensure_owner(session, owner):
    policy = session.get(Policy, owner)
    if policy is None:
        policy = Policy(owner_id=owner)
        session.add(policy)
    for marketplace, name in (("wb", "Wildberries"), ("ozon", "Ozon")):
        if session.scalar(select(Account.id).where(Account.owner_id == owner, Account.marketplace == marketplace)) is None:
            session.add(Account(owner_id=owner, marketplace=marketplace, name=name))
    session.flush()
    return policy


def enqueue(session, owner, marketplace=None, kind="sync", trigger="manual", target_product_id=None):
    active = session.get(ActiveJob, owner)
    if active:
        return session.get(Job, active.job_id)
    job = Job(owner_id=owner, marketplace=marketplace, kind=kind, trigger=trigger, target_product_id=target_product_id)
    session.add(job)
    session.flush()
    session.add(ActiveJob(owner_id=owner, job_id=job.id))
    session.flush()
    log(session, owner, "job_queued", job.id, kind=kind, marketplace=marketplace)
    return job


def unlink(session, mapping):
    # Manual singletons suppress automatic relinking on subsequent syncs.
    if mapping.wb_product_id and mapping.ozon_product_id:
        session.add(Mapping(owner_id=mapping.owner_id, ozon_product_id=mapping.ozon_product_id, mapping_type="manual"))
        mapping.ozon_product_id = None
    mapping.mapping_type = "manual"
    mapping.status = "mapping_required"
    mapping.updated_at = now()
    session.flush()


def manual_link(session, owner, wb_id, ozon_id):
    products = list(session.scalars(select(Product).where(Product.id.in_([wb_id, ozon_id]), Product.owner_id == owner)))
    if len(products) != 2 or {p.marketplace for p in products} != {"wb", "ozon"}:
        raise ValueError("Выберите один свой товар Wildberries и один товар Ozon.")
    lookup = {p.id: p for p in products}
    if lookup[wb_id].marketplace != "wb" or lookup[ozon_id].marketplace != "ozon":
        raise ValueError("Неверные площадки для сопоставления.")
    affected = list(session.scalars(select(Mapping).where(Mapping.owner_id == owner, (Mapping.wb_product_id == wb_id) | (Mapping.ozon_product_id == ozon_id))))
    for mapping in affected:
        unlink(session, mapping)
    session.flush()
    for mapping in list(session.scalars(select(Mapping).where(Mapping.owner_id == owner, (Mapping.wb_product_id == wb_id) | (Mapping.ozon_product_id == ozon_id)))):
        session.delete(mapping)
    session.flush()
    result = Mapping(owner_id=owner, wb_product_id=wb_id, ozon_product_id=ozon_id, mapping_type="manual")
    session.add(result)
    session.flush()
    log(session, owner, "mapping_created", mapping_id=result.id, mapping_type="manual")
    recalculate(session, owner)
    return result


def automatic_mappings(session, owner, job_id):
    products = list(session.scalars(select(Product).where(Product.owner_id == owner)))
    mapping_by_product = {}
    mappings = list(session.scalars(select(Mapping).where(Mapping.owner_id == owner)))
    for mapping in mappings:
        for product in (mapping.wb_product_id, mapping.ozon_product_id):
            if product:
                mapping_by_product[product] = mapping
    for product in products:
        if product.id not in mapping_by_product:
            mapping = Mapping(owner_id=owner, **{f"{product.marketplace}_product_id": product.id})
            session.add(mapping)
            mapping_by_product[product.id] = mapping
    session.flush()
    by_id = {product.id: product for product in products}
    for mapping in mappings:
        if mapping.mapping_type == "automatic" and mapping.wb_product_id and mapping.ozon_product_id:
            wb, ozon = by_id[mapping.wb_product_id], by_id[mapping.ozon_product_id]
            if wb.present and ozon.present and (not wb.seller_article or wb.seller_article != ozon.seller_article):
                mapping.ozon_product_id = None
                session.flush()
                right = Mapping(owner_id=owner, ozon_product_id=ozon.id)
                session.add(right)
                session.flush()
                mapping_by_product[ozon.id] = right
                log(session, owner, "mapping_conflict", job_id, mapping_id=mapping.id, reason="seller_article_changed")
    groups = defaultdict(lambda: {"wb": [], "ozon": []})
    for product in products:
        if product.present and product.seller_article:
            groups[product.seller_article][product.marketplace].append(product)
    for article, candidates in groups.items():
        ambiguous = any(len(candidates[marketplace]) > 1 for marketplace in ("wb", "ozon"))
        if ambiguous:
            split = set()
            for product in candidates["wb"] + candidates["ozon"]:
                mapping = mapping_by_product[product.id]
                if mapping.id not in split and mapping.mapping_type == "automatic" and mapping.wb_product_id and mapping.ozon_product_id:
                    split.add(mapping.id)
                    ozon_id = mapping.ozon_product_id
                    mapping.ozon_product_id = None
                    session.flush()
                    new = Mapping(owner_id=owner, ozon_product_id=ozon_id)
                    session.add(new)
                    session.flush()
                    mapping_by_product[ozon_id] = new
                    log(session, owner, "mapping_conflict", job_id, mapping_id=mapping.id, seller_article=article)
            continue
        if len(candidates["wb"]) != 1 or len(candidates["ozon"]) != 1:
            continue
        wb, ozon = candidates["wb"][0], candidates["ozon"][0]
        left, right = mapping_by_product[wb.id], mapping_by_product[ozon.id]
        if left is right or left.mapping_type == "manual" or right.mapping_type == "manual":
            continue
        if left.ozon_product_id or right.wb_product_id:
            continue
        session.delete(right)
        session.flush()
        left.ozon_product_id = ozon.id
        left.updated_at = now()
        mapping_by_product[ozon.id] = left
        log(session, owner, "mapping_created", job_id, mapping_id=left.id, mapping_type="automatic")
    session.flush()


def is_stale(product, account, policy, at):
    if policy.price_source == "storefront":
        return (product.price_source != "storefront" or product.price_context_version != policy.context_version
                or product.collection_status != "success" or not product.updated_at
                or product.updated_at < at - timedelta(hours=policy.stale_hours))
    return (not product.updated_at or product.updated_at < at - timedelta(hours=policy.stale_hours)
            or not account.last_successful_sync_at
            or account.credential_version != account.synced_credential_version
            or bool(account.last_failed_sync_at and account.last_failed_sync_at > account.last_successful_sync_at))


def recalculate(session, owner, job_id=None):
    policy = session.get(Policy, owner)
    products = {p.id: p for p in session.scalars(select(Product).where(Product.owner_id == owner))}
    accounts = {a.id: a for a in session.scalars(select(Account).where(Account.owner_id == owner))}
    snapshots = {s.id: s for s in session.scalars(select(Snapshot).join(Product, Product.current_snapshot_id == Snapshot.id).where(Product.owner_id == owner))}
    comparisons = {(c.mapping_id, c.price_type): c for c in session.scalars(select(Comparison).join(Mapping).where(Mapping.owner_id == owner))}
    for mapping in session.scalars(select(Mapping).where(Mapping.owner_id == owner)):
        wb, ozon = products.get(mapping.wb_product_id), products.get(mapping.ozon_product_id)
        status = None
        if not wb or not ozon:
            status = "mapping_required"
        elif not wb.present:
            status = "missing_wb"
        elif not ozon.present:
            status = "missing_ozon"
        elif any(is_stale(p, accounts[p.account_id], policy, now()) for p in (wb, ozon)):
            status = "stale"
        unequal, comparable = False, 0
        for key in PRICE_TYPES:
            comparison = comparisons.get((mapping.id, key))
            if comparison is None:
                comparison = Comparison(mapping_id=mapping.id, price_type=key, status="unavailable")
                session.add(comparison)
            comparison.signed_difference = comparison.difference_absolute = comparison.difference_percent = comparison.direction = None
            left, right = snapshots.get(wb.current_snapshot_id) if wb else None, snapshots.get(ozon.current_snapshot_id) if ozon else None
            a, b = getattr(left, key) if left else None, getattr(right, key) if right else None
            if status:
                comparison.status = status
            elif policy.price_source == "storefront" and key != policy.price_basis:
                comparison.status = "not_compared"
            elif a is None or b is None or not left.currency or left.currency != right.currency:
                comparison.status = "unavailable"
            else:
                signed, percent = difference(a, b)
                comparable += 1
                unequal |= signed != 0
                comparison.status = "mismatch" if signed else "matched"
                comparison.signed_difference, comparison.difference_absolute = signed, abs(signed)
                comparison.difference_percent = abs(percent) if percent is not None else None
                comparison.direction = "wb_cheaper" if signed > 0 else "ozon_cheaper" if signed < 0 else "equal"
        mapping.status = status or ("mismatch" if unequal else "matched" if comparable else "price_unavailable")
    if job_id:
        log(session, owner, "comparison_recalculated", job_id)


def promote(session, account, job, captured):
    updated, snapshots = 0, 0
    products = {p.external_id: p for p in session.scalars(select(Product).where(Product.account_id == account.id))}
    seen = set()
    policy = session.get(Policy, account.owner_id)
    zone = ZoneInfo(policy.timezone)
    for stage in session.scalars(select(StagedProduct).where(StagedProduct.job_id == job.id, StagedProduct.account_id == account.id).execution_options(yield_per=1000)):
        payload = stage.payload
        seen.add(stage.external_id)
        product = products.get(stage.external_id)
        if not product:
            product = Product(owner_id=account.owner_id, account_id=account.id, marketplace=account.marketplace, external_id=stage.external_id,
                              seller_article=payload["seller_article"], name=payload["name"], sku=payload["sku"], status=payload["status"])
            session.add(product)
            session.flush()
            log(session, account.owner_id, "product_discovered", job.id, product.id)
        product.seller_article, product.name = payload["seller_article"], payload["name"]
        product.sku, product.status, product.present, product.updated_at = payload["sku"], payload["status"], True, captured
        price = NormalizedPrice.from_payload(payload["price"])
        previous = session.get(Snapshot, product.current_snapshot_id) if product.current_snapshot_id else None
        changed = not previous or any(getattr(previous, key) != getattr(price, key) for key in PRICE_TYPES) or previous.currency != price.currency or previous.sources != price.sources or previous.reasons != price.reasons
        different_day = not previous or previous.captured_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone).date() != captured.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone).date()
        if changed or different_day:
            snapshot = Snapshot(product_id=product.id, job_id=job.id, captured_at=captured,
                                **{key: getattr(price, key) for key in PRICE_TYPES}, currency=price.currency, sources=price.sources, reasons=price.reasons)
            session.add(snapshot)
            session.flush()
            product.current_snapshot_id = snapshot.id
            snapshots += 1
            log(session, account.owner_id, "price_snapshot_created", job.id, product.id, snapshot_id=snapshot.id)
            if changed and previous:
                changes = {key: {"old": str(getattr(previous, key)) if getattr(previous, key) is not None else None,
                                 "new": str(getattr(price, key)) if getattr(price, key) is not None else None,
                                 "difference": str(getattr(price, key) - getattr(previous, key)) if getattr(price, key) is not None and getattr(previous, key) is not None else None}
                           for key in PRICE_TYPES if getattr(previous, key) != getattr(price, key)}
                log(session, account.owner_id, "price_changed", job.id, product.id, changes=changes)
        updated += 1
    for external, product in products.items():
        if external not in seen:
            product.present = False
    account.last_successful_sync_at, account.last_sync_at = captured, captured
    account.synced_credential_version = account.credential_version
    account.status, account.last_error = "connected", None
    return updated, snapshots


def claim(factory, settings):
    with factory() as session:
        write_lock(session)
        expired = list(session.scalars(select(Job).where(Job.kind != "browser", Job.finished_at.is_(None), Job.started_at.is_not(None), Job.heartbeat_at < now() - timedelta(seconds=settings.lease_seconds))))
        for job in expired:
            job.status, job.phase, job.finished_at = "failed", "interrupted", now()
            job.errors_count, job.error_summary = job.errors_count + 1, ERRORS["interrupted"]
            for account in session.scalars(select(Account).where(Account.owner_id == job.owner_id)):
                if job.kind == "sync" and (job.marketplace is None or job.marketplace == account.marketplace):
                    account.last_failed_sync_at, account.last_error = now(), job.error_summary
            session.execute(delete(ActiveJob).where(ActiveJob.job_id == job.id))
            session.execute(delete(StagedProduct).where(StagedProduct.job_id == job.id))
            for product in session.scalars(select(Product).where(Product.owner_id == job.owner_id, Product.collection_status == "pending")):
                product.collection_status, product.collection_error = "failed", "interrupted"
            recalculate(session, job.owner_id)
            log(session, job.owner_id, "sync_finish", job.id, status="failed", error_code="interrupted")
        job = session.scalar(select(Job).where(Job.kind != "browser", Job.finished_at.is_(None), Job.started_at.is_(None)).order_by(Job.id).limit(1))
        if job:
            job.started_at = job.heartbeat_at = now()
            job.phase = "starting"
            log(session, job.owner_id, "sync_start", job.id)
        session.commit()
        return job.id if job else None


def run_job(factory, settings, job_id, adapter_factory=None, collector_factory=None):
    with factory() as session:
        pending = session.get(Job, job_id)
        policy = session.get(Policy, pending.owner_id)
        use_storefront = policy.price_source == "storefront" and pending.kind in ("sync", "probe")
        use_catalog = pending.kind == "catalog"
    if use_storefront or use_catalog:
        from .collection import run_storefront_job, run_catalog_job
        return (run_storefront_job(factory, settings, job_id, collector_factory)
                if use_storefront else run_catalog_job(factory, settings, job_id, adapter_factory))
    def heartbeat():
        with factory() as session:
            result = session.execute(update(Job).where(Job.id == job_id, Job.finished_at.is_(None)).values(heartbeat_at=now()))
            if not result.rowcount:
                raise ApiError("interrupted")
            session.commit()

    with factory() as session:
        job = session.get(Job, job_id)
        owner, kind = job.owner_id, job.kind
        accounts = list(session.scalars(select(Account).where(Account.owner_id == owner)))
        accounts = [a for a in accounts if job.marketplace is None or job.marketplace == a.marketplace]
    results = {}
    for account in accounts:
        adapter = None
        try:
            heartbeat()
            with factory() as session:
                session.execute(update(Job).where(Job.id == job_id).values(phase=f"{kind}_{account.marketplace}"))
                session.commit()
            if not account.secret:
                raise ApiError("not_configured")
            token = decrypt(settings, account.secret)
            adapter = adapter_factory(account) if adapter_factory else (
                WildberriesAdapter(settings, token, heartbeat=heartbeat) if account.marketplace == "wb" else OzonAdapter(settings, account.client_id, token, heartbeat=heartbeat))
            if kind == "probe":
                capabilities = adapter.probe()
                success = all(c["status"] == "available" for c in capabilities.values())
                with factory() as session:
                    write_lock(session)
                    current = session.get(Account, account.id)
                    if current.credential_version != account.credential_version:
                        raise ApiError("credentials_changed")
                    if session.get(Job, job_id).finished_at:
                        raise ApiError("interrupted")
                    current.capabilities, current.last_checked_at = capabilities, now()
                    current.status = "connected" if success else "failed"
                    current.last_error = None if success else " ".join(ERRORS[c["error"]] for c in capabilities.values() if "error" in c)
                    session.commit()
                results[account.marketplace] = {"status": "success" if success else "failed", "capabilities": capabilities}
                continue
            received = 0
            with factory() as session:
                for product in adapter.products():
                    session.add(StagedProduct(job_id=job_id, account_id=account.id, external_id=product.external_id, payload=product.payload()))
                    received += 1
                    if received % 1000 == 0:
                        session.commit()
                        heartbeat()
                session.commit()
            with factory() as session:
                write_lock(session)
                current = session.get(Account, account.id)
                current_job = session.get(Job, job_id)
                if current_job.finished_at:
                    raise ApiError("interrupted")
                if current.credential_version != account.credential_version:
                    raise ApiError("credentials_changed")
                captured = now()
                updated, prices = promote(session, current, current_job, captured)
                current_job.products_received += received
                current_job.products_updated += updated
                current_job.prices_updated += prices
                session.execute(delete(StagedProduct).where(StagedProduct.job_id == job_id, StagedProduct.account_id == account.id))
                session.commit()
            results[account.marketplace] = {"status": "success", "products_received": received, "products_updated": updated, "prices_updated": prices}
        except Exception as exc:
            code = exc.code if isinstance(exc, ApiError) else "credential_unreadable" if isinstance(exc, ValueError) and str(exc) == "credential_unreadable" else "contract" if isinstance(exc, IntegrityError) else "internal"
            with factory() as session:
                write_lock(session)
                current = session.get(Account, account.id)
                current_job = session.get(Job, job_id)
                if current_job.finished_at:
                    return
                if current.credential_version == account.credential_version:
                    current.status, current.last_error = "failed", ERRORS[code]
                    if kind == "sync":
                        current.last_sync_at = current.last_failed_sync_at = now()
                session.execute(delete(StagedProduct).where(StagedProduct.job_id == job_id, StagedProduct.account_id == account.id))
                log(session, owner, "api_error", job_id, marketplace=account.marketplace, code=code, http_status=getattr(exc, "http_status", None))
                session.commit()
            results[account.marketplace] = {"status": "failed", "error_code": code, "message": ERRORS[code]}
        finally:
            if adapter:
                adapter.close()
    with factory() as session:
        write_lock(session)
        job = session.get(Job, job_id)
        if job.finished_at:
            return
        job.phase = "comparing"
        automatic_mappings(session, owner, job.id)
        recalculate(session, owner, job.id)
        successes = sum(r["status"] == "success" for r in results.values())
        job.status = "success" if successes == len(results) else "partial" if successes else "failed"
        job.results, job.errors_count = results, len(results) - successes
        job.error_summary = summarize_errors(results) or ("Проверка прав API завершилась ошибкой." if any(r["status"] == "failed" for r in results.values()) else None)
        job.phase, job.finished_at, job.heartbeat_at = "complete", now(), now()
        session.execute(delete(ActiveJob).where(ActiveJob.job_id == job_id))
        log(session, owner, "sync_finish", job_id, status=job.status)
        session.commit()


def schedule_due(factory):
    with factory() as session:
        write_lock(session)
        for policy in session.scalars(select(Policy).where(Policy.enabled.is_(True), Policy.daily_time.is_not(None))):
            local = now().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(policy.timezone))
            date = local.date().isoformat()
            if local.strftime("%H:%M") >= policy.daily_time and policy.last_scheduled_date != date and not session.get(ActiveJob, policy.owner_id):
                enqueue(session, policy.owner_id, trigger="scheduled")
                policy.last_scheduled_date = date
        session.commit()
