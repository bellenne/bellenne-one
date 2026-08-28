from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session

from app.models import MonthlyPlan, PlanAllocation, PlanMode, PlanValue, ProductGroup
from app.services.metrics import MONEY_FIELDS, PLAN_INPUT_FIELDS, decimal, derived_values, safe_divide


@dataclass(frozen=True)
class WeekSlice:
    number: int
    start: date
    end: date
    days_in_month: int


def month_start(value: date) -> date:
    return value.replace(day=1)


def days_in_month(value: date) -> int:
    return calendar.monthrange(value.year, value.month)[1]


def month_dates(value: date) -> list[date]:
    first = month_start(value)
    return [first + timedelta(days=offset) for offset in range(days_in_month(first))]


def calendar_weeks(value: date) -> list[WeekSlice]:
    dates = month_dates(value)
    grouped: list[list[date]] = []
    for current in dates:
        if not grouped or current.weekday() == 0:
            grouped.append([])
        grouped[-1].append(current)
    return [WeekSlice(index + 1, days[0], days[-1], len(days)) for index, days in enumerate(grouped)]


def rolling_six_week_window(value: date) -> tuple[date, date]:
    first = month_start(value)
    start = first - timedelta(days=first.weekday())
    return start, start + timedelta(days=41)


def derived_plan_values(values: dict[str, object]) -> dict[str, Decimal | None]:
    """Calculate ratios without rounding fractional unit targets."""
    result = derived_values(values)
    ordered_units = decimal(values.get("ordered_units"))
    buyout_units = decimal(values.get("buyout_units"))
    ordered_amount = decimal(values.get("ordered_amount"))
    result["ordered_units"] = ordered_units
    result["buyout_units"] = buyout_units
    result["average_order_value"] = safe_divide(ordered_amount, ordered_units)
    result["buyout_rate"] = safe_divide(buyout_units, ordered_units)
    return result


def normalize_plan_input(metric_key: str, value: object) -> Decimal:
    parsed = max(decimal(value), Decimal("0"))
    if metric_key in MONEY_FIELDS:
        return parsed.quantize(Decimal("0.01"))
    return parsed.quantize(Decimal("1"))


def parse_plan_payload(form: dict[str, str], groups: list[ProductGroup]) -> tuple[str, dict[str, Decimal], dict[int, dict[str, Decimal]], dict[int, Decimal]]:
    mode = form.get("mode", PlanMode.TOTAL.value)
    total_values = {field: normalize_plan_input(field, form.get(f"total_{field}")) for field in PLAN_INPUT_FIELDS}
    group_values: dict[int, dict[str, Decimal]] = {}
    allocations: dict[int, Decimal] = {}
    for group in groups:
        allocations[group.id] = max(decimal(form.get(f"allocation_{group.id}")), Decimal("0")).quantize(Decimal("0.01"))
        group_values[group.id] = {
            field: normalize_plan_input(field, form.get(f"group_{group.id}_{field}"))
            for field in PLAN_INPUT_FIELDS
        }
    return mode, total_values, group_values, allocations


def validate_plan(mode: str, allocations: dict[int, Decimal], group_values: dict[int, dict[str, Decimal]]) -> list[str]:
    errors: list[str] = []
    if mode == PlanMode.TOTAL.value:
        allocation_sum = sum(allocations.values(), Decimal("0"))
        if allocations and abs(allocation_sum - Decimal("100")) > Decimal("0.01"):
            errors.append(f"Сумма долей товаров должна быть 100%, сейчас {allocation_sum.normalize()}%.")
    elif mode == PlanMode.BY_PRODUCT.value:
        if not group_values:
            errors.append("Добавьте хотя бы один тип товара для индивидуального плана.")
    else:
        errors.append("Неизвестный режим плана.")
    return errors


def save_plan(
    session: Session,
    user_id: int,
    period: date,
    marketplace: str,
    mode: str,
    total_values: dict[str, Decimal],
    group_values: dict[int, dict[str, Decimal]],
    allocations: dict[int, Decimal],
) -> MonthlyPlan:
    period = month_start(period)
    plan = session.scalar(
        select(MonthlyPlan).where(
            MonthlyPlan.user_id == user_id,
            MonthlyPlan.period == period,
            MonthlyPlan.marketplace == marketplace,
        )
    )
    if plan is None:
        plan = MonthlyPlan(user_id=user_id, period=period, marketplace=marketplace, mode=mode)
        session.add(plan)
        session.flush()
    plan.mode = mode
    session.execute(delete(PlanValue).where(PlanValue.plan_id == plan.id))
    session.execute(delete(PlanAllocation).where(PlanAllocation.plan_id == plan.id))

    if mode == PlanMode.TOTAL.value:
        for metric_key, value in total_values.items():
            session.add(
                PlanValue(
                    plan_id=plan.id,
                    product_group_id=None,
                    scope_key="total",
                    metric_key=metric_key,
                    monthly_value=value,
                )
            )
        for group_id, percentage in allocations.items():
            session.add(PlanAllocation(plan_id=plan.id, product_group_id=group_id, percentage=percentage))
    else:
        total_by_metric = {field: Decimal("0") for field in PLAN_INPUT_FIELDS}
        for group_id, values in group_values.items():
            for metric_key, value in values.items():
                total_by_metric[metric_key] += value
                session.add(
                    PlanValue(
                        plan_id=plan.id,
                        product_group_id=group_id,
                        scope_key=f"group:{group_id}",
                        metric_key=metric_key,
                        monthly_value=value,
                    )
                )
        for metric_key, value in total_by_metric.items():
            session.add(
                PlanValue(
                    plan_id=plan.id,
                    product_group_id=None,
                    scope_key="total",
                    metric_key=metric_key,
                    monthly_value=value,
                )
            )
    session.commit()
    session.refresh(plan)
    return plan


def load_plan(session: Session, user_id: int, period: date, marketplace: str = "ALL") -> MonthlyPlan | None:
    period = month_start(period)
    exact = session.scalar(
        select(MonthlyPlan).where(
            MonthlyPlan.user_id == user_id,
            MonthlyPlan.period == period,
            MonthlyPlan.marketplace == marketplace,
        )
    )
    return exact


def plan_monthly_values(plan: MonthlyPlan | None, product_group_id: int | None = None) -> dict[str, Decimal | int | None]:
    values = {field: Decimal("0") for field in PLAN_INPUT_FIELDS}
    if plan is None:
        return derived_plan_values(values)
    if product_group_id is None:
        selected = [item for item in plan.values if item.scope_key == "total"]
    elif plan.mode == PlanMode.BY_PRODUCT.value:
        selected = [item for item in plan.values if item.product_group_id == product_group_id]
    else:
        totals = {item.metric_key: decimal(item.monthly_value) for item in plan.values if item.scope_key == "total"}
        allocation = next((decimal(item.percentage) for item in plan.allocations if item.product_group_id == product_group_id), Decimal("0"))
        selected = []
        for metric_key in PLAN_INPUT_FIELDS:
            values[metric_key] = totals.get(metric_key, Decimal("0")) * allocation / Decimal("100")
        return derived_plan_values(values)
    for item in selected:
        values[item.metric_key] = decimal(item.monthly_value)
    return derived_plan_values(values)


def plan_for_range(monthly: dict[str, Decimal | int | None], period: date, start: date, end: date) -> dict[str, Decimal | int | None]:
    first = month_start(period)
    last = first.replace(day=days_in_month(first))
    overlap_start = max(first, start)
    overlap_end = min(last, end)
    count = max((overlap_end - overlap_start).days + 1, 0)
    divisor = Decimal(days_in_month(first))
    scaled: dict[str, object] = {}
    for field in PLAN_INPUT_FIELDS:
        raw_value = monthly.get(field)
        if field == "gross_profit" and raw_value is None:
            scaled[field] = None
        else:
            scaled[field] = decimal(raw_value) * Decimal(count) / divisor
    return derived_plan_values(scaled)


def daily_plan(monthly: dict[str, Decimal | int | None], period: date, target: date) -> dict[str, Decimal | int | None]:
    return plan_for_range(monthly, period, target, target)
