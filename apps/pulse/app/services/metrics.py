from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import DailyMetric, MarketplaceAccount, MonthlyPlan, PlanValue, ProductGroup


MONEY_FIELDS = {"ordered_amount", "buyout_amount", "net_revenue", "ad_spend", "ad_bonus_spend", "gross_profit"}
PLAN_INPUT_FIELDS = (
    "ordered_units",
    "ordered_amount",
    "buyout_units",
    "buyout_amount",
    "net_revenue",
    "ad_spend",
    "ad_bonus_spend",
    "gross_profit",
)

METRIC_LABELS = {
    "ordered_units": "Заказано (шт)",
    "ordered_amount": "Сумма заказов (руб)",
    "average_order_value": "Средний чек заказа",
    "buyout_units": "Выкуплено (шт)",
    "buyout_amount": "Сумма выкупов (руб)",
    "net_revenue": "Итоговая выручка после снятия комиссий (руб)",
    "ad_spend": "РК Денег",
    "ad_bonus_spend": "РК Бонусов",
    "drr_buyouts_total": "Общий ДРРв (%)",
    "drr_orders_total": "Общий ДРРз (%)",
    "drr_buyouts_cash": "Денег ДРРв (%)",
    "drr_orders_cash": "Денег ДРРз (%)",
    "gross_profit": "Валовая прибыль",
}

REPORT_METRIC_ORDER = (
    "ordered_units",
    "ordered_amount",
    "average_order_value",
    "buyout_units",
    "buyout_amount",
    "net_revenue",
    "ad_spend",
    "ad_bonus_spend",
    "drr_buyouts_total",
    "drr_orders_total",
    "drr_buyouts_cash",
    "drr_orders_cash",
    "gross_profit",
)


def decimal(value: object | None) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def safe_divide(numerator: Decimal | int, denominator: Decimal | int) -> Decimal | None:
    den = decimal(denominator)
    if den == 0:
        return None
    return decimal(numerator) / den


def derived_values(values: dict[str, object]) -> dict[str, Decimal | int | None]:
    ordered_units = int(decimal(values.get("ordered_units")))
    buyout_units = int(decimal(values.get("buyout_units")))
    ordered_amount = decimal(values.get("ordered_amount"))
    buyout_amount = decimal(values.get("buyout_amount"))
    cash = decimal(values.get("ad_spend"))
    bonus = decimal(values.get("ad_bonus_spend"))
    gross_profit = values.get("gross_profit")
    return {
        **values,
        "ordered_units": ordered_units,
        "buyout_units": buyout_units,
        "ordered_amount": ordered_amount,
        "buyout_amount": buyout_amount,
        "net_revenue": decimal(values.get("net_revenue")),
        "ad_spend": cash,
        "ad_bonus_spend": bonus,
        "average_order_value": safe_divide(ordered_amount, ordered_units),
        "buyout_rate": safe_divide(buyout_units, ordered_units),
        "drr_buyouts_total": safe_divide(cash + bonus, buyout_amount),
        "drr_orders_total": safe_divide(cash + bonus, ordered_amount),
        "drr_buyouts_cash": safe_divide(cash, buyout_amount),
        "drr_orders_cash": safe_divide(cash, ordered_amount),
        # Gross profit is deliberately never inferred from marketplace data.
        "gross_profit": gross_profit if gross_profit is not None else None,
    }


def metric_query(
    user_id: int,
    date_from: date,
    date_to: date,
    marketplace: str = "ALL",
    account_id: int | None = None,
    product_group_id: int | None = None,
) -> Select:
    conditions = [
        DailyMetric.user_id == user_id,
        DailyMetric.metric_date >= date_from,
        DailyMetric.metric_date <= date_to,
    ]
    if marketplace != "ALL":
        conditions.append(DailyMetric.marketplace == marketplace)
    if account_id:
        conditions.append(DailyMetric.account_id == account_id)
    if product_group_id:
        conditions.extend(
            [DailyMetric.product_group_id == product_group_id, DailyMetric.scope_key != "total"]
        )
    else:
        conditions.append(DailyMetric.scope_key == "total")
    return select(DailyMetric).where(and_(*conditions)).order_by(DailyMetric.metric_date)


def aggregate_rows(rows: Iterable[DailyMetric]) -> dict[str, Decimal | int | None]:
    totals: dict[str, object] = {
        "ordered_units": 0,
        "ordered_amount": Decimal("0"),
        "buyout_units": 0,
        "buyout_amount": Decimal("0"),
        "net_revenue": Decimal("0"),
        "ad_spend": Decimal("0"),
        "ad_bonus_spend": Decimal("0"),
        "gross_profit": None,
    }
    for row in rows:
        totals["ordered_units"] = int(totals["ordered_units"]) + row.ordered_units
        totals["buyout_units"] = int(totals["buyout_units"]) + row.buyout_units
        for field in ("ordered_amount", "buyout_amount", "net_revenue", "ad_spend", "ad_bonus_spend"):
            totals[field] = decimal(totals[field]) + decimal(getattr(row, field))
    return derived_values(totals)


def daily_series(rows: Iterable[DailyMetric], date_from: date, date_to: date) -> list[dict[str, object]]:
    grouped: dict[date, list[DailyMetric]] = {}
    for row in rows:
        grouped.setdefault(row.metric_date, []).append(row)
    result = []
    current = date_from
    while current <= date_to:
        result.append({"date": current, **aggregate_rows(grouped.get(current, []))})
        current += timedelta(days=1)
    return result


def compare_value(actual: Decimal | int | None, plan: Decimal | int | None) -> Decimal | None:
    if actual is None or plan is None:
        return None
    return safe_divide(decimal(actual), decimal(plan))
