from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.clock import utc_now
from app.config import Settings
from app.models import (
    DailyMetric,
    Marketplace,
    MarketplaceAccount,
    MarketplaceProduct,
    ProductGroup,
    SyncRun,
)
from app.security import decrypt_credentials
from app.services.integrations import DemoIntegration, OzonIntegration, SyncPayload, WildberriesIntegration
from app.services.integrations.base import IntegrationError, MetricPayload
from app.services.metrics import decimal
from app.services.wb_summary import apply_wb_summary_override


logger = logging.getLogger(__name__)

_ADVERTISING_RESIDUAL_SCOPE = "unallocated:advertising"
_AUTHORITATIVE_ADVERTISING_FIELDS = frozenset({"ad_spend", "ad_bonus_spend"})
_PRODUCT_NAME_GROUP_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Фотообои", ("фотообо", "обои", "обоев", "обоям", "обоями", "обоях")),
    ("Футболки", ("футболк", "футболок")),
    ("Фотосетка", ("фотосетк", "фотофасад")),
)


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zа-яё0-9]+", "-", value.lower(), flags=re.IGNORECASE).strip("-")
    return normalized[:100] or "product"


def product_group_from_name(name: str) -> str | None:
    """Return a canonical product group when the product name is unambiguous."""
    normalized = name.casefold().replace("ё", "е")
    for group_name, markers in _PRODUCT_NAME_GROUP_RULES:
        if any(marker in normalized for marker in markers):
            return group_name
    return None


def _product_group_by_name(session: Session, user_id: int, group_name: str) -> ProductGroup | None:
    exact_match = session.scalar(
        select(ProductGroup).where(
            ProductGroup.user_id == user_id,
            ProductGroup.name == group_name,
        )
    )
    if exact_match is not None:
        return exact_match

    normalized_name = group_name.casefold()
    return next(
        (
            group
            for group in session.scalars(
                select(ProductGroup).where(ProductGroup.user_id == user_id)
            )
            if group.name.casefold() == normalized_name
        ),
        None,
    )


def _get_or_create_product_group(session: Session, user_id: int, group_name: str) -> ProductGroup:
    group = _product_group_by_name(session, user_id, group_name)
    if group is None:
        group = ProductGroup(user_id=user_id, name=group_name, code=slugify(group_name))
        session.add(group)
        session.flush()
    return group


def integration_for(account: MarketplaceAccount, credentials: dict[str, str], settings: Settings):
    if account.is_demo:
        return DemoIntegration(account.marketplace)
    if account.marketplace == Marketplace.WB.value:
        return WildberriesIntegration(credentials, settings.request_timeout_seconds)
    if account.marketplace == Marketplace.OZON.value:
        return OzonIntegration(credentials, settings.request_timeout_seconds)
    raise IntegrationError(f"Неизвестный маркетплейс: {account.marketplace}")


def _group_for_product(session: Session, account: MarketplaceAccount, external_id: str, name: str, category: str | None, offer_id: str | None) -> tuple[MarketplaceProduct, ProductGroup]:
    product = session.scalar(
        select(MarketplaceProduct).where(
            MarketplaceProduct.account_id == account.id,
            MarketplaceProduct.external_id == external_id,
        )
    )
    detected_group_name = product_group_from_name(name)
    if product and product.product_group and detected_group_name is None:
        product.name = name
        product.category = category
        product.offer_id = offer_id
        return product, product.product_group

    group_name = (detected_group_name or category or name or f"Товар {external_id}").strip()[:120]
    group = _get_or_create_product_group(session, account.user_id, group_name)
    if product is None:
        product = MarketplaceProduct(
            account_id=account.id,
            product_group_id=group.id,
            external_id=external_id,
            offer_id=offer_id,
            name=name,
            category=category,
        )
        session.add(product)
    else:
        product.product_group_id = group.id
        product.name = name
        product.category = category
        product.offer_id = offer_id
    session.flush()
    return product, group


def _upsert_metric(
    session: Session,
    account: MarketplaceAccount,
    target_date: date,
    scope_key: str,
    payload: MetricPayload,
    product_group_id: int | None,
    source: str,
) -> DailyMetric:
    row = session.scalar(
        select(DailyMetric).where(
            DailyMetric.account_id == account.id,
            DailyMetric.metric_date == target_date,
            DailyMetric.scope_key == scope_key,
        )
    )
    if row is None:
        row = DailyMetric(
            user_id=account.user_id,
            account_id=account.id,
            marketplace=account.marketplace,
            metric_date=target_date,
            scope_key=scope_key,
            product_group_id=product_group_id,
        )
        session.add(row)
    row.product_group_id = product_group_id
    row.ordered_units = payload.ordered_units
    row.ordered_amount = payload.ordered_amount
    row.buyout_units = payload.buyout_units
    row.buyout_amount = payload.buyout_amount
    row.net_revenue = payload.net_revenue
    row.ad_spend = payload.ad_spend
    row.ad_bonus_spend = payload.ad_bonus_spend
    row.ctr = payload.ctr
    row.gross_profit = None
    row.source = source
    row.collected_at = utc_now()
    return row


def persist_payload(session: Session, account: MarketplaceAccount, target_date: date, payload: SyncPayload) -> int:
    by_group: dict[int, MetricPayload] = {}
    active_total = MetricPayload()
    reported_product_total = MetricPayload()
    for product_payload in payload.products:
        reported_product_total.add(product_payload.metrics)
        product, group = _group_for_product(
            session,
            account,
            product_payload.external_id,
            product_payload.name,
            product_payload.category,
            product_payload.offer_id,
        )
        if product.is_active:
            by_group.setdefault(group.id, MetricPayload()).add(product_payload.metrics)
            if group.is_active:
                active_total.add(product_payload.metrics)

    active_scope_keys = [f"group:{group_id}" for group_id in by_group]
    stale_group_rows = delete(DailyMetric).where(
        DailyMetric.account_id == account.id,
        DailyMetric.metric_date == target_date,
        DailyMetric.scope_key != "total",
    )
    if active_scope_keys:
        stale_group_rows = stale_group_rows.where(DailyMetric.scope_key.not_in(active_scope_keys))
    session.execute(stale_group_rows)
    for group_id, metrics in by_group.items():
        _upsert_metric(
            session,
            account,
            target_date,
            f"group:{group_id}",
            metrics,
            group_id,
            "demo" if account.is_demo else "api",
        )
    advertising_residual = MetricPayload()
    authoritative_fields = payload.authoritative_total_fields & _AUTHORITATIVE_ADVERTISING_FIELDS
    for field_name in authoritative_fields:
        authoritative_value = decimal(getattr(payload.total, field_name))
        allocated_value = decimal(getattr(reported_product_total, field_name))
        residual_value = max(authoritative_value - allocated_value, Decimal("0"))
        setattr(advertising_residual, field_name, residual_value)
        setattr(
            active_total,
            field_name,
            decimal(getattr(active_total, field_name)) + residual_value,
        )
    if advertising_residual.ad_spend or advertising_residual.ad_bonus_spend:
        _upsert_metric(
            session,
            account,
            target_date,
            _ADVERTISING_RESIDUAL_SCOPE,
            advertising_residual,
            None,
            "api",
        )
    _upsert_metric(
        session,
        account,
        target_date,
        "total",
        active_total if payload.products else payload.total,
        None,
        "demo" if account.is_demo else "api",
    )
    session.flush()
    if account.marketplace == Marketplace.WB.value and not account.is_demo:
        apply_wb_summary_override(session, account, target_date)
    return len(by_group) + 1 + int(bool(advertising_residual.ad_spend or advertising_residual.ad_bonus_spend))


def rebuild_user_totals(session: Session, user_id: int) -> int:
    """Recalculate stored totals after a whole product group is enabled or disabled."""
    session.flush()
    active_group_ids = set(
        session.scalars(
            select(ProductGroup.id).where(
                ProductGroup.user_id == user_id,
                ProductGroup.is_active.is_(True),
            )
        )
    )
    group_rows = list(
        session.scalars(
            select(DailyMetric).where(
                DailyMetric.user_id == user_id,
                DailyMetric.scope_key != "total",
                DailyMetric.scope_key != _ADVERTISING_RESIDUAL_SCOPE,
            )
        )
    )
    totals_by_scope: dict[tuple[int, date], MetricPayload] = {}
    for row in group_rows:
        if row.product_group_id not in active_group_ids:
            continue
        key = (row.account_id, row.metric_date)
        totals_by_scope.setdefault(key, MetricPayload()).add(
            MetricPayload(
                ordered_units=row.ordered_units,
                ordered_amount=row.ordered_amount,
                buyout_units=row.buyout_units,
                buyout_amount=row.buyout_amount,
                net_revenue=row.net_revenue,
                ad_spend=row.ad_spend,
                ad_bonus_spend=row.ad_bonus_spend,
            )
        )

    residual_rows = list(
        session.scalars(
            select(DailyMetric).where(
                DailyMetric.user_id == user_id,
                DailyMetric.scope_key == _ADVERTISING_RESIDUAL_SCOPE,
            )
        )
    )
    for row in residual_rows:
        key = (row.account_id, row.metric_date)
        totals_by_scope.setdefault(key, MetricPayload()).add(
            MetricPayload(
                ad_spend=row.ad_spend,
                ad_bonus_spend=row.ad_bonus_spend,
            )
        )

    total_rows = list(
        session.scalars(
            select(DailyMetric).where(
                DailyMetric.user_id == user_id,
                DailyMetric.scope_key == "total",
            )
        )
    )
    for row in total_rows:
        payload = totals_by_scope.get((row.account_id, row.metric_date), MetricPayload())
        row.ordered_units = payload.ordered_units
        row.ordered_amount = payload.ordered_amount
        row.buyout_units = payload.buyout_units
        row.buyout_amount = payload.buyout_amount
        row.net_revenue = payload.net_revenue
        row.ad_spend = payload.ad_spend
        row.ad_bonus_spend = payload.ad_bonus_spend
        row.ctr = None
        row.gross_profit = None
        row.collected_at = utc_now()
        account = session.get(MarketplaceAccount, row.account_id)
        if account and account.marketplace == Marketplace.WB.value and not account.is_demo:
            apply_wb_summary_override(session, account, row.metric_date)
    return len(total_rows)


async def sync_account(session: Session, account: MarketplaceAccount, target_date: date, cipher: Fernet, settings: Settings) -> SyncRun:
    run = SyncRun(account_id=account.id, target_date=target_date, status="running")
    session.add(run)
    session.commit()
    try:
        credentials = decrypt_credentials(cipher, account.encrypted_credentials)
        integration = integration_for(account, credentials, settings)
        payload = await integration.fetch(target_date)
        rows = persist_payload(session, account, target_date, payload)
        message = "; ".join(payload.warnings) if payload.warnings else "Метрики обновлены"
        run.status = "success" if not payload.warnings else "warning"
        run.rows_written = rows
        run.message = message
        account.last_sync_status = run.status
        account.last_sync_message = message
    except Exception as exc:
        session.rollback()
        run = session.get(SyncRun, run.id)
        account = session.get(MarketplaceAccount, account.id)
        run.status = "error"
        run.message = str(exc)[:2000]
        account.last_sync_status = "error"
        account.last_sync_message = str(exc)[:2000]
        logger.exception("Marketplace sync failed for account %s", account.id)
    finally:
        run.finished_at = utc_now()
        account.last_sync_at = utc_now()
        session.commit()
    return run
