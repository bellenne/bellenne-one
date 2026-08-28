from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import DailyMetric, MarketplaceAccount, ProductGroup
from app.services.metrics import aggregate_rows, daily_series


def report_rows(
    session: Session,
    user_id: int,
    date_from: date,
    date_to: date,
    marketplace: str = "ALL",
    account_id: int | None = None,
    product_group_id: int | None = None,
    totals_only: bool = True,
) -> list[DailyMetric]:
    conditions = [
        DailyMetric.user_id == user_id,
        DailyMetric.metric_date >= date_from,
        DailyMetric.metric_date <= date_to,
    ]
    if marketplace != "ALL":
        conditions.append(DailyMetric.marketplace == marketplace)
    if account_id:
        conditions.append(DailyMetric.account_id == account_id)
    if product_group_id is not None:
        active_group = session.scalar(
            select(ProductGroup.id).where(
                ProductGroup.id == product_group_id,
                ProductGroup.user_id == user_id,
                ProductGroup.is_active.is_(True),
            )
        )
        if active_group is None:
            return []
        conditions.extend(
            [DailyMetric.product_group_id == product_group_id, DailyMetric.scope_key != "total"]
        )
    elif totals_only:
        conditions.append(DailyMetric.scope_key == "total")
    return list(session.scalars(select(DailyMetric).where(and_(*conditions)).order_by(DailyMetric.metric_date)))


def report_summary(
    session: Session,
    user_id: int,
    date_from: date,
    date_to: date,
    marketplace: str = "ALL",
    account_id: int | None = None,
    product_group_id: int | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    rows = report_rows(
        session,
        user_id,
        date_from,
        date_to,
        marketplace,
        account_id,
        product_group_id,
        totals_only=product_group_id is None,
    )
    return aggregate_rows(rows), daily_series(rows, date_from, date_to)


def available_accounts(session: Session, user_id: int) -> list[MarketplaceAccount]:
    return list(
        session.scalars(
            select(MarketplaceAccount)
            .where(MarketplaceAccount.user_id == user_id)
            .order_by(MarketplaceAccount.marketplace, MarketplaceAccount.name)
        )
    )


def available_groups(session: Session, user_id: int) -> list[ProductGroup]:
    return list(
        session.scalars(
            select(ProductGroup)
            .where(ProductGroup.user_id == user_id, ProductGroup.is_active.is_(True))
            .order_by(ProductGroup.name)
        )
    )


def all_groups(session: Session, user_id: int) -> list[ProductGroup]:
    return list(
        session.scalars(
            select(ProductGroup)
            .where(ProductGroup.user_id == user_id)
            .order_by(ProductGroup.name)
        )
    )
