from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    DailyMetric,
    MarketplaceProduct,
    MonthlyPlan,
    PlanAllocation,
    PlanValue,
    ProductGroup,
)
from app.services.metrics import decimal
from app.services.sync import rebuild_user_totals


@dataclass(frozen=True)
class ProductGroupMergeResult:
    target_name: str
    categories_merged: int
    products_moved: int
    daily_rows_merged: int
    plan_rows_merged: int
    totals_rebuilt: int


_INTEGER_METRIC_FIELDS = (
    "ordered_units",
    "buyout_units",
    "tests_count",
    "tests_passed",
    "liquidated_units",
)

_DECIMAL_METRIC_FIELDS = (
    "ordered_amount",
    "buyout_amount",
    "net_revenue",
    "ad_spend",
    "ad_bonus_spend",
    "test_spend",
    "liquidated_amount",
)


def _merge_daily_metric(target: DailyMetric, source: DailyMetric) -> None:
    for field_name in _INTEGER_METRIC_FIELDS:
        setattr(target, field_name, int(getattr(target, field_name) or 0) + int(getattr(source, field_name) or 0))
    for field_name in _DECIMAL_METRIC_FIELDS:
        setattr(
            target,
            field_name,
            decimal(getattr(target, field_name)) + decimal(getattr(source, field_name)),
        )
    if target.gross_profit is not None or source.gross_profit is not None:
        target.gross_profit = decimal(target.gross_profit) + decimal(source.gross_profit)
    target.ctr = None
    collected = [value for value in (target.collected_at, source.collected_at) if isinstance(value, datetime)]
    if collected:
        target.collected_at = max(collected)


def merge_product_groups(
    session: Session,
    user_id: int,
    target_group_id: int,
    source_group_ids: list[int],
) -> ProductGroupMergeResult:
    source_ids = list(dict.fromkeys(group_id for group_id in source_group_ids if group_id != target_group_id))
    if not source_ids:
        raise ValueError("Выберите хотя бы одну исходную категорию, отличную от итоговой.")

    requested_ids = {target_group_id, *source_ids}
    groups = list(
        session.scalars(
            select(ProductGroup).where(
                ProductGroup.user_id == user_id,
                ProductGroup.id.in_(requested_ids),
            )
        )
    )
    groups_by_id = {group.id: group for group in groups}
    if requested_ids != set(groups_by_id):
        raise ValueError("Одна или несколько выбранных категорий не найдены.")

    target_group = groups_by_id[target_group_id]
    source_groups = [groups_by_id[group_id] for group_id in source_ids]

    products = list(
        session.scalars(
            select(MarketplaceProduct).where(MarketplaceProduct.product_group_id.in_(source_ids))
        )
    )
    for product in products:
        product.product_group_id = target_group.id

    plan_values = list(
        session.scalars(
            select(PlanValue)
            .join(MonthlyPlan)
            .where(
                MonthlyPlan.user_id == user_id,
                PlanValue.product_group_id.in_(requested_ids),
            )
            .order_by(PlanValue.id)
        )
    )
    target_values: dict[tuple[int, str], PlanValue] = {
        (value.plan_id, value.metric_key): value
        for value in plan_values
        if value.product_group_id == target_group.id
    }
    plan_rows_merged = 0
    for value in plan_values:
        if value.product_group_id not in source_ids:
            continue
        key = (value.plan_id, value.metric_key)
        target_value = target_values.get(key)
        if target_value is None:
            value.product_group_id = target_group.id
            value.scope_key = f"group:{target_group.id}"
            target_values[key] = value
        else:
            target_value.monthly_value = decimal(target_value.monthly_value) + decimal(value.monthly_value)
            session.delete(value)
        plan_rows_merged += 1

    allocations = list(
        session.scalars(
            select(PlanAllocation)
            .join(MonthlyPlan)
            .where(
                MonthlyPlan.user_id == user_id,
                PlanAllocation.product_group_id.in_(requested_ids),
            )
            .order_by(PlanAllocation.id)
        )
    )
    target_allocations: dict[int, PlanAllocation] = {
        allocation.plan_id: allocation
        for allocation in allocations
        if allocation.product_group_id == target_group.id
    }
    for allocation in allocations:
        if allocation.product_group_id not in source_ids:
            continue
        target_allocation = target_allocations.get(allocation.plan_id)
        if target_allocation is None:
            allocation.product_group_id = target_group.id
            target_allocations[allocation.plan_id] = allocation
        else:
            target_allocation.percentage = decimal(target_allocation.percentage) + decimal(allocation.percentage)
            session.delete(allocation)
        plan_rows_merged += 1

    metric_rows = list(
        session.scalars(
            select(DailyMetric)
            .where(DailyMetric.product_group_id.in_(requested_ids))
            .order_by(DailyMetric.account_id, DailyMetric.metric_date, DailyMetric.id)
        )
    )
    target_metrics: dict[tuple[int, object], DailyMetric] = {
        (row.account_id, row.metric_date): row
        for row in metric_rows
        if row.product_group_id == target_group.id
    }
    daily_rows_merged = 0
    for row in metric_rows:
        if row.product_group_id not in source_ids:
            continue
        key = (row.account_id, row.metric_date)
        target_row = target_metrics.get(key)
        if target_row is None:
            row.product_group_id = target_group.id
            row.scope_key = f"group:{target_group.id}"
            target_metrics[key] = row
        else:
            _merge_daily_metric(target_row, row)
            session.delete(row)
        daily_rows_merged += 1

    session.flush()
    for group in source_groups:
        session.delete(group)
    session.flush()
    totals_rebuilt = rebuild_user_totals(session, user_id)

    return ProductGroupMergeResult(
        target_name=target_group.name,
        categories_merged=len(source_groups),
        products_moved=len(products),
        daily_rows_merged=daily_rows_merged,
        plan_rows_merged=plan_rows_merged,
        totals_rebuilt=totals_rebuilt,
    )
