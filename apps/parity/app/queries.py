from datetime import timedelta
from decimal import Decimal
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import aliased
from .domain import PRICE_TYPES, difference, now
from .models import Account, Comparison, Mapping, Policy, Product, Snapshot


def catalog(session, owner, *, q="", exact=False, status="", price_type="", direction="", min_percent=None, updated_since=None, sort="updated", page=1, page_size=50, availability=""):
    wb, ozon = aliased(Product), aliased(Product)
    wa, oa = aliased(Account), aliased(Account)
    policy = session.get(Policy, owner)
    cutoff = now() - timedelta(hours=policy.stale_hours)
    stale_fields = (or_(wb.updated_at < cutoff, ozon.updated_at < cutoff,
        wb.price_source != "storefront", ozon.price_source != "storefront",
        wb.price_context_version != policy.context_version, ozon.price_context_version != policy.context_version,
        wb.collection_status != "success", ozon.collection_status != "success") if policy.price_source == "storefront" else or_(
        wb.updated_at < cutoff, ozon.updated_at < cutoff,
        wa.last_successful_sync_at.is_(None), oa.last_successful_sync_at.is_(None),
        wa.credential_version != wa.synced_credential_version, oa.credential_version != oa.synced_credential_version,
        wa.last_failed_sync_at > wa.last_successful_sync_at, oa.last_failed_sync_at > oa.last_successful_sync_at))
    expired = and_(Mapping.wb_product_id.is_not(None), Mapping.ozon_product_id.is_not(None), wb.present.is_(True), ozon.present.is_(True), stale_fields)
    effective = case((expired, "stale"), else_=Mapping.status)
    base = select(Mapping, wb, ozon, effective.label("effective_status")).outerjoin(wb, Mapping.wb_product_id == wb.id).outerjoin(ozon, Mapping.ozon_product_id == ozon.id).outerjoin(wa, wb.account_id == wa.id).outerjoin(oa, ozon.account_id == oa.id).where(Mapping.owner_id == owner)
    if q:
        fields = (wb.seller_article, ozon.seller_article, wb.external_id, ozon.external_id, wb.sku, ozon.sku, wb.name, ozon.name)
        base = base.where(or_(*[(field == q if exact else field.contains(q, autoescape=True)) for field in fields]))
    if status:
        base = base.where(effective == status)
    if availability == "both":
        base = base.where(wb.present.is_(True), ozon.present.is_(True))
    elif availability == "missing_wb":
        base = base.where(or_(wb.id.is_(None), wb.present.is_(False)))
    elif availability == "missing_ozon":
        base = base.where(or_(ozon.id.is_(None), ozon.present.is_(False)))
    comparison_filter = [Comparison.mapping_id == Mapping.id, Comparison.status == "mismatch"]
    if price_type:
        comparison_filter.append(Comparison.price_type == price_type)
    if direction:
        comparison_filter.append(Comparison.direction == direction)
    if min_percent is not None:
        comparison_filter.append(Comparison.difference_percent >= Decimal(str(min_percent)))
    if price_type or direction or min_percent is not None:
        base = base.where(select(Comparison.id).where(*comparison_filter).exists())
    updated = case((wb.updated_at.is_(None), ozon.updated_at), (ozon.updated_at.is_(None), wb.updated_at), (wb.updated_at > ozon.updated_at, wb.updated_at), else_=ozon.updated_at)
    if updated_since:
        base = base.where(updated >= updated_since)
    total = session.scalar(select(func.count()).select_from(base.subquery()))
    ordering = {"updated": updated.desc(), "article": func.coalesce(wb.seller_article, ozon.seller_article).asc(), "name": func.coalesce(wb.name, ozon.name).asc(),
                "difference": select(func.max(Comparison.difference_absolute)).where(Comparison.mapping_id == Mapping.id).scalar_subquery().desc()}.get(sort, updated.desc())
    ordered = base.order_by(ordering, Mapping.id)
    if page_size is not None:
        ordered = ordered.offset((page - 1) * page_size).limit(page_size)
    result = session.execute(ordered).all()
    snapshot_ids = [p.current_snapshot_id for row in result for p in (row[1], row[2]) if p and p.current_snapshot_id]
    snapshots = {s.id: s for s in session.scalars(select(Snapshot).where(Snapshot.id.in_(snapshot_ids)))}
    mapping_ids = [row[0].id for row in result]
    comparisons = {}
    for comparison in session.scalars(select(Comparison).where(Comparison.mapping_id.in_(mapping_ids))):
        comparisons.setdefault(comparison.mapping_id, {})[comparison.price_type] = comparison
    def current_price(product):
        snapshot = snapshots.get(product.current_snapshot_id) if product else None
        if snapshot and policy.price_source == "storefront" and (snapshot.source_kind != "storefront" or snapshot.context_version != policy.context_version):
            return None
        return snapshot
    return {"total": total, "page": page, "page_size": page_size, "pages": max(1, (total + page_size - 1) // page_size) if page_size else 1, "rows": [
        {"mapping": mapping, "wb": left, "ozon": right, "status": state,
         "wb_price": current_price(left),
         "ozon_price": current_price(right),
         "comparisons": comparisons.get(mapping.id, {}),
         "updated_at": max([p.updated_at for p in (left, right) if p] or [mapping.updated_at])}
        for mapping, left, right, state in result]}


def history(session, mapping, days=30, page=1, page_size=100, source_kind=None, context_version=None):
    ids = [i for i in (mapping.wb_product_id, mapping.ozon_product_id) if i]
    query = select(Snapshot, Product.marketplace).join(Product, Snapshot.product_id == Product.id).where(Snapshot.product_id.in_(ids))
    if source_kind:
        query = query.where(Snapshot.source_kind == source_kind)
    if context_version is not None:
        query = query.where(Snapshot.context_version == context_version)
    if days:
        query = query.where(Snapshot.captured_at >= now() - timedelta(days=days))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    entries = session.execute(query.order_by(Snapshot.captured_at.desc(), Snapshot.id.desc()).offset((page-1)*page_size).limit(page_size)).all()
    previous = aliased(Snapshot)
    def prior(field):
        return select(field).where(previous.product_id == Snapshot.product_id, previous.source_kind == Snapshot.source_kind, previous.context_version == Snapshot.context_version, or_(
            previous.captured_at < Snapshot.captured_at,
            and_(previous.captured_at == Snapshot.captured_at, previous.id < Snapshot.id),
        )).order_by(previous.captured_at.desc(), previous.id.desc()).limit(1).correlate(Snapshot).scalar_subquery()
    changes = {}
    if entries:
        result = session.execute(select(Snapshot.id, prior(previous.id), prior(previous.currency), *[prior(getattr(previous,key)) for key in PRICE_TYPES])
                                 .where(Snapshot.id.in_([entry[0].id for entry in entries]))).all()
        by_id = {entry[0].id: entry[0] for entry in entries}
        for snapshot_id, previous_id, previous_currency, *old_prices in result:
            snapshot = by_id[snapshot_id]
            values = []
            if previous_id:
                for key, old in zip(PRICE_TYPES, old_prices):
                    new = getattr(snapshot,key)
                    if old != new or previous_currency != snapshot.currency:
                        delta, percent = difference(old,new) if old is not None and new is not None and previous_currency == snapshot.currency else (None,None)
                        values.append({"price_type":key, "old":old,"new":new,"difference":delta,"percent":percent,"old_currency":previous_currency})
            changes[snapshot_id] = {"initial": previous_id is None, "values": values}
    # Exact history is paginated. Chart covers only the requested window and is
    # bounded separately; large windows ask the user to narrow the period.
    chart_limit = 5000
    chart = session.execute(query.order_by(Snapshot.captured_at, Snapshot.id).limit(chart_limit+1)).all()
    truncated = len(chart) > chart_limit
    return {"entries": entries, "changes": changes, "total": total, "pages": max(1, (total+page_size-1)//page_size), "page": page,
            "truncated": truncated, "chart": [] if truncated else [
                {"marketplace": marketplace, "job_id": snapshot.job_id, "source_kind": snapshot.source_kind, "context_version": snapshot.context_version, "captured_at": snapshot.captured_at.isoformat()+"Z", "currency": snapshot.currency,
                 **{key: str(getattr(snapshot, key)) if getattr(snapshot, key) is not None else None for key in ("base_price", "discount_price", "loyalty_price")}}
                for snapshot, marketplace in chart]}
