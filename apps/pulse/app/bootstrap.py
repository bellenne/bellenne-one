from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import utc_now
from app.config import Settings
from app.database import Base
from app.models import DailyMetric, Marketplace, MarketplaceAccount, MonthlyPlan, ProductGroup, User
from app.services.integrations import DemoIntegration
from app.services.metrics import aggregate_rows
from app.services.planning import save_plan
from app.services.sync import persist_payload


async def initialize_database(engine, session_factory, settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)


async def ensure_demo_data(session: Session, user: User, settings: Settings) -> None:
    accounts = list(
        session.scalars(
            select(MarketplaceAccount).where(
                MarketplaceAccount.user_id == user.id,
                MarketplaceAccount.is_demo.is_(True),
            )
        )
    )
    by_market = {account.marketplace: account for account in accounts}
    for marketplace, name in ((Marketplace.WB.value, "WB · Демо"), (Marketplace.OZON.value, "Ozon · Демо")):
        if marketplace not in by_market:
            account = MarketplaceAccount(
                user_id=user.id,
                name=name,
                marketplace=marketplace,
                is_demo=True,
                schedule_enabled=False,
                timezone=settings.timezone,
                last_sync_status="success",
                last_sync_message="Демонстрационные данные",
            )
            session.add(account)
            session.flush()
            by_market[marketplace] = account
    session.commit()

    if not settings.seed_demo_data:
        return
    metric_count = session.scalar(select(func.count(DailyMetric.id)).where(DailyMetric.user_id == user.id)) or 0
    today = date.today()
    if metric_count == 0:
        start = today.replace(day=1) - timedelta(days=42)
        current = start
        while current <= today:
            for marketplace, account in by_market.items():
                payload = await DemoIntegration(marketplace).fetch(current)
                persist_payload(session, account, current, payload)
            current += timedelta(days=1)
        for account in by_market.values():
            account.last_sync_at = utc_now()
        session.commit()

    groups = list(
        session.scalars(
            select(ProductGroup)
            .where(ProductGroup.user_id == user.id)
            .order_by(ProductGroup.name)
        )
    )
    days = calendar.monthrange(today.year, today.month)[1]
    for marketplace in (Marketplace.WB.value, Marketplace.OZON.value):
        existing = session.scalar(
            select(MonthlyPlan).where(
                MonthlyPlan.user_id == user.id,
                MonthlyPlan.period == today.replace(day=1),
                MonthlyPlan.marketplace == marketplace,
            )
        )
        if existing:
            continue
        rows = list(
            session.scalars(
                select(DailyMetric).where(
                    DailyMetric.user_id == user.id,
                    DailyMetric.marketplace == marketplace,
                    DailyMetric.metric_date >= today.replace(day=1),
                    DailyMetric.metric_date <= today,
                    DailyMetric.scope_key == "total",
                )
            )
        )
        actual = aggregate_rows(rows)
        elapsed = max(today.day, 1)
        factor = Decimal(days) / Decimal(elapsed) * Decimal("1.08")
        total_values = {
            "ordered_units": Decimal(int(Decimal(actual["ordered_units"]) * factor)),
            "ordered_amount": Decimal(actual["ordered_amount"]) * factor,
            "buyout_units": Decimal(int(Decimal(actual["buyout_units"]) * factor)),
            "buyout_amount": Decimal(actual["buyout_amount"]) * factor,
            "net_revenue": Decimal(actual["net_revenue"]) * factor,
            "ad_spend": Decimal(actual["ad_spend"]) * factor,
            "ad_bonus_spend": Decimal(actual["ad_bonus_spend"]) * factor,
        }
        allocations = {}
        if groups:
            raw = [Decimal("70"), Decimal("20"), Decimal("10")]
            for index, group in enumerate(groups):
                allocations[group.id] = raw[index] if index < len(raw) else Decimal("0")
            allocation_sum = sum(allocations.values(), Decimal("0"))
            if allocation_sum != Decimal("100"):
                allocations[groups[0].id] += Decimal("100") - allocation_sum
        save_plan(
            session,
            user.id,
            today,
            marketplace,
            "total",
            total_values,
            {},
            allocations,
        )
