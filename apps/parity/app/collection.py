"""Durable jobs: catalogue APIs provide identifiers; rendered pages provide prices."""
from pathlib import Path
from uuid import uuid4
from sqlalchemy import delete, select, update
from .adapters import ApiError, ERRORS as API_ERRORS, OzonAdapter, WildberriesAdapter
from .domain import PRICE_TYPES, now
from .models import Account, ActiveJob, CollectionAttempt, Job, Policy, Product, Snapshot, StagedProduct
from .security import decrypt
from .service import automatic_mappings, log, recalculate, summarize_errors, write_lock
from .storefront import CollectionError, ERRORS, StorefrontCollector


def heartbeat(factory, job_id):
    with factory() as session:
        result = session.execute(update(Job).where(Job.id == job_id, Job.finished_at.is_(None)).values(heartbeat_at=now()))
        if not result.rowcount:
            raise ApiError("interrupted")
        session.commit()


def finish(factory, job_id, results):
    with factory() as session:
        write_lock(session)
        job = session.get(Job, job_id)
        if job.finished_at:
            return
        if job.kind in ("sync", "probe"):
            for product in session.scalars(select(Product).where(Product.owner_id == job.owner_id, Product.collection_status == "pending")):
                product.collection_status, product.collection_error = "failed", "internal"
        automatic_mappings(session, job.owner_id, job.id)
        recalculate(session, job.owner_id, job.id)
        successful = sum(r.get("successes", 0) for r in results.values())
        job.status = "success" if results and all(r["status"] == "success" for r in results.values()) else "partial" if successful else "failed"
        job.results = results
        job.error_summary = summarize_errors(results)
        job.phase, job.finished_at, job.heartbeat_at = "complete", now(), now()
        session.execute(delete(ActiveJob).where(ActiveJob.job_id == job_id))
        log(session, job.owner_id, "sync_finish", job_id, status=job.status)
        session.commit()


def run_storefront_job(factory, settings, job_id, collector_factory=None):
    with factory() as session:
        job = session.get(Job, job_id)
        owner = job.owner_id
        policy = session.get(Policy, owner)
        query = select(Product).where(Product.owner_id == owner, Product.present.is_(True)).order_by(Product.marketplace, Product.id)
        if job.marketplace:
            query = query.where(Product.marketplace == job.marketplace)
        if job.target_product_id:
            query = query.where(Product.id == job.target_product_id)
        if job.kind == "probe":
            query = query.limit(1)
        products = list(session.scalars(query))
        markets = [job.marketplace] if job.marketplace else ["wb", "ozon"]
        # Interrupted jobs must not leave previously collected prices looking fresh.
        for product in products:
            product.collection_status, product.collection_error = "pending", None
        recalculate(session, owner)
        session.commit()
    results = {}
    collector = None
    try:
        collector = collector_factory(settings, policy, lambda: heartbeat(factory, job_id)) if collector_factory else StorefrontCollector(settings, policy, lambda: heartbeat(factory, job_id))
        for market in markets:
            selected = [p for p in products if p.marketplace == market]
            successes, failures, blocked_code, attempted, last_code = 0, 0, None, 0, None
            with factory() as session:
                session.get(Job, job_id).phase = f"storefront_{market}"
                session.commit()
            for product in selected:
                heartbeat(factory, job_id)
                observation, code, evidence_key, diagnostic = None, blocked_code, None, {}
                if not blocked_code:
                    attempted += 1
                    try:
                        observation = collector.collect(product)
                        if observation.evidence:
                            try:
                                folder = settings.data_dir / "observations"
                                folder.mkdir(parents=True, exist_ok=True)
                                candidate = uuid4().hex + ".jpg"
                                (folder / candidate).write_bytes(observation.evidence)
                                evidence_key = candidate
                            except OSError:
                                pass
                    except Exception as exc:
                        code = exc.code if isinstance(exc, CollectionError) else "internal"
                        diagnostic = exc.details if isinstance(exc, CollectionError) else {}
                        if code in ("blocked", "browser", "navigation", "internal"):
                            blocked_code = code
                with factory() as session:
                    write_lock(session)
                    current_job = session.get(Job, job_id)
                    if current_job.finished_at:
                        return
                    current = session.get(Product, product.id)
                    if observation:
                        snapshot = Snapshot(product_id=product.id, job_id=job_id, source_kind="storefront", context_version=policy.context_version,
                                            context=observation.context, page_url=observation.page_url, evidence_key=evidence_key,
                                            **{key: getattr(observation.price, key) for key in PRICE_TYPES},
                                            currency=observation.price.currency, sources=observation.price.sources, reasons=observation.price.reasons)
                        session.add(snapshot)
                        session.flush()
                        current.current_snapshot_id, current.updated_at = snapshot.id, snapshot.captured_at
                        current.price_source, current.price_context_version = "storefront", policy.context_version
                        current.collection_status, current.collection_error = "success", None
                        current_job.prices_updated += 1
                        current_job.products_updated += 1
                        successes += 1
                        log(session, owner, "price_snapshot_created", job_id, product.id, source="storefront", city=policy.city)
                    else:
                        failures += 1
                        last_code = code
                        current.collection_status, current.collection_error = "failed", code
                        current_job.errors_count += 1
                        log(session, owner, "collection_error", job_id, product.id, marketplace=market, code=code, skipped=bool(blocked_code and attempted < successes + failures), diagnostic=diagnostic)
                    current_job.products_received = sum(r.get("attempted", 0) for r in results.values()) + attempted
                    session.add(CollectionAttempt(owner_id=owner, product_id=product.id, job_id=job_id,
                                                  status=current.collection_status, error_code=code,
                                                  details={"city": policy.city, "context_version": policy.context_version, **diagnostic}))
                    session.commit()
            if not selected:
                failures, blocked_code = 1, "no_products"
                with factory() as session:
                    session.get(Job, job_id).errors_count += 1
                    session.commit()
            results[market] = {"status": "success" if not failures else "partial" if successes else "failed",
                               "successes": successes, "errors": failures, "attempted": attempted, "skipped": len(selected) - attempted,
                               "message": (ERRORS.get(blocked_code or (last_code if len(selected) == 1 else None)) or "Часть карточек не прочитана. Причины доступны в карточках товаров и журнале.") if failures else None}
    except Exception:
        results["collector"] = {"status": "failed", "successes": 0, "message": ERRORS["internal"]}
    finally:
        if collector:
            try:
                collector.close()
            except Exception:
                pass
    finish(factory, job_id, results)


def run_catalog_job(factory, settings, job_id, adapter_factory=None):
    with factory() as session:
        job = session.get(Job, job_id)
        accounts = list(session.scalars(select(Account).where(Account.owner_id == job.owner_id)))
        accounts = [a for a in accounts if not job.marketplace or a.marketplace == job.marketplace]
    results = {}
    for account in accounts:
        adapter = None
        try:
            if not account.secret:
                raise ApiError("not_configured")
            adapter = adapter_factory(account) if adapter_factory else (WildberriesAdapter(settings, decrypt(settings, account.secret), heartbeat=lambda: heartbeat(factory, job_id))
                         if account.marketplace == "wb" else OzonAdapter(settings, account.client_id, decrypt(settings, account.secret), heartbeat=lambda: heartbeat(factory, job_id)))
            with factory() as session:
                session.get(Job, job_id).phase = f"catalog_{account.marketplace}"
                session.commit()
            received = 0
            for product in adapter.products(include_prices=False):
                with factory() as session:
                    session.add(StagedProduct(job_id=job_id, account_id=account.id, external_id=product.external_id, payload=product.payload()))
                    session.commit()
                received += 1
            with factory() as session:
                write_lock(session)
                current_job, current_account = session.get(Job, job_id), session.get(Account, account.id)
                if current_job.finished_at:
                    return
                if current_account.credential_version != account.credential_version:
                    raise ApiError("credentials_changed")
                existing = {p.external_id: p for p in session.scalars(select(Product).where(Product.account_id == account.id))}
                seen = set()
                for staged in session.scalars(select(StagedProduct).where(StagedProduct.job_id == job_id, StagedProduct.account_id == account.id)):
                    data = staged.payload
                    current = existing.get(staged.external_id)
                    if not current:
                        current = Product(owner_id=account.owner_id, account_id=account.id, marketplace=account.marketplace, external_id=staged.external_id)
                        session.add(current)
                    if current.sku != data["sku"]:
                        current.collection_status, current.collection_error = "not_collected", None
                    for key in ("sku", "seller_article", "name", "status"):
                        setattr(current, key, data[key])
                    current.present = True
                    seen.add(staged.external_id)
                for external, current in existing.items():
                    if external not in seen:
                        current.present = False
                current_account.status, current_account.last_error = "connected", None
                current_account.last_checked_at = now()
                current_account.capabilities = {"products": {"status": "available"}}
                current_job.products_received += received
                current_job.products_updated += received
                session.execute(delete(StagedProduct).where(StagedProduct.job_id == job_id, StagedProduct.account_id == account.id))
                session.commit()
            results[account.marketplace] = {"status": "success", "successes": received, "products_received": received}
        except Exception as exc:
            code = exc.code if isinstance(exc, ApiError) else "credential_unreadable" if isinstance(exc, ValueError) and str(exc) == "credential_unreadable" else "internal"
            with factory() as session:
                session.get(Job, job_id).errors_count += 1
                current_account = session.get(Account, account.id)
                current_account.status, current_account.last_error = "failed", API_ERRORS[code]
                session.execute(delete(StagedProduct).where(StagedProduct.job_id == job_id, StagedProduct.account_id == account.id))
                log(session, account.owner_id, "api_error", job_id, marketplace=account.marketplace, code=code)
                session.commit()
            results[account.marketplace] = {"status": "failed", "successes": 0, "message": API_ERRORS[code]}
        finally:
            if adapter:
                adapter.close()
    finish(factory, job_id, results)
