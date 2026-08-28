from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.metrics import derived_values
from app.services.planning import calendar_weeks, daily_plan, plan_for_range, rolling_six_week_window


def test_month_plan_divides_by_calendar_days():
    monthly = derived_values(
        {
            "ordered_units": 310,
            "ordered_amount": Decimal("310000"),
            "buyout_units": 155,
            "buyout_amount": Decimal("155000"),
            "net_revenue": Decimal("100000"),
            "ad_spend": Decimal("31000"),
            "ad_bonus_spend": Decimal("0"),
        }
    )
    day = daily_plan(monthly, date(2026, 8, 1), date(2026, 8, 18))
    assert day["ordered_units"] == 10
    assert day["ordered_amount"] == Decimal("10000")
    assert day["gross_profit"] is None


def test_gross_profit_plan_divides_without_creating_actual():
    monthly = derived_values({"gross_profit": Decimal("3100.00")})
    day = daily_plan(monthly, date(2026, 8, 1), date(2026, 8, 18))
    assert day["gross_profit"] == Decimal("100.00")


def test_fractional_daily_unit_targets_sum_back_to_month():
    monthly = derived_values({"ordered_units": 100, "buyout_units": 67})
    days = [daily_plan(monthly, date(2026, 8, 1), date(2026, 8, day)) for day in range(1, 32)]
    assert abs(sum((row["ordered_units"] for row in days), Decimal("0")) - Decimal("100")) < Decimal("1e-20")
    assert abs(sum((row["buyout_units"] for row in days), Decimal("0")) - Decimal("67")) < Decimal("1e-20")


def test_week_plan_uses_only_days_inside_month():
    monthly = derived_values({"ordered_units": 310, "ordered_amount": 310000})
    first_rolling_week = plan_for_range(monthly, date(2026, 8, 1), date(2026, 7, 27), date(2026, 8, 2))
    assert first_rolling_week["ordered_units"] == 20
    assert first_rolling_week["ordered_amount"] == Decimal("20000")


def test_six_week_window_matches_reference_pattern():
    start, end = rolling_six_week_window(date(2026, 8, 1))
    assert start == date(2026, 7, 27)
    assert end == date(2026, 9, 6)
    assert (end - start).days == 41


def test_calendar_weeks_cover_month_without_gaps():
    weeks = calendar_weeks(date(2028, 2, 1))
    assert sum(week.days_in_month for week in weeks) == 29
    assert weeks[0].start == date(2028, 2, 1)
    assert weeks[-1].end == date(2028, 2, 29)
