from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Campaign, DailyStat, Product

CHART_COLORS = (
    "var(--bp-blue)",
    "var(--bp-violet)",
    "var(--bp-cyan)",
    "var(--bp-violet-secondary)",
    "var(--bp-text-secondary)",
)

COUNT_FIELDS = (
    "views",
    "clicks",
    "atbs",
    "orders",
    "canceled",
    "shks",
)


def _empty_metrics() -> dict[str, float | int]:
    return {
        "views": 0,
        "clicks": 0,
        "spend": 0.0,
        "atbs": 0,
        "orders": 0,
        "canceled": 0,
        "shks": 0,
        "revenue": 0.0,
    }


def _add_metrics(target: dict[str, Any], row: Any) -> None:
    for field in COUNT_FIELDS:
        target[field] += int(getattr(row, field) or 0)
    target["spend"] += float(row.spend or 0)
    target["revenue"] += float(row.revenue or 0)


def _ratio(numerator: float | int, denominator: float | int) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def _complete_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    result = dict(metrics)
    views = int(result["views"])
    clicks = int(result["clicks"])
    atbs = int(result["atbs"])
    orders = int(result["orders"])
    spend = float(result["spend"])
    revenue = float(result["revenue"])
    canceled = int(result["canceled"])
    result.update(
        {
            "cpm": 1000 * _ratio(spend, views),
            "ctr": _ratio(clicks, views),
            "cpc": _ratio(spend, clicks),
            "cart_cr": _ratio(atbs, clicks),
            "order_cr": _ratio(orders, atbs),
            "cpo": _ratio(spend, orders),
            "drr": _ratio(spend, revenue),
            "roas": _ratio(revenue, spend),
            "cancel_rate": _ratio(canceled, orders),
        }
    )
    return result


def _display_label(value: str, fallback: str) -> str:
    clean = (value or "").strip()
    if not clean:
        return fallback
    return clean[0].upper() + clean[1:]


def _dimension_key(value: str) -> str:
    return value.strip().casefold()


def _performance_zones(group: str, metrics: dict[str, Any]) -> dict[str, str]:
    is_shirt = "футбол" in group.casefold()
    limits = {
        "ctr": (0.045, 0.025) if is_shirt else (0.035, 0.025),
        "cpc": (10.0, 16.0) if is_shirt else (13.0, 20.0),
        "cart_cr": (0.11, 0.07) if is_shirt else (0.10, 0.07),
        "order_cr": (0.27, 0.21) if is_shirt else (0.09, 0.06),
        "drr": (0.10, 0.15),
    }
    zones = {}
    for metric, (good_limit, warning_limit) in limits.items():
        value = float(metrics[metric])
        lower_is_better = metric in {"cpc", "drr"}
        if lower_is_better:
            zones[metric] = (
                "good"
                if value < good_limit
                else ("warning" if value <= warning_limit else "bad")
            )
        else:
            zones[metric] = (
                "good"
                if value >= good_limit
                else ("warning" if value >= warning_limit else "bad")
            )
    return zones


def _delta(current: float | int, previous: float | int) -> float | None:
    if not previous:
        return None
    return 100 * (float(current) - float(previous)) / abs(float(previous))


def _build_line_chart(daily: list[dict[str, Any]]) -> dict[str, Any]:
    width = 1000
    height = 300
    left = 58
    right = 58
    top = 22
    bottom = 42
    plot_width = width - left - right
    plot_height = height - top - bottom
    revenue_max = max((float(row["revenue"]) for row in daily), default=0)
    spend_max = max((float(row["spend"]) for row in daily), default=0)
    revenue_scale = revenue_max or 1
    spend_scale = spend_max or 1
    denominator = max(len(daily) - 1, 1)
    points = []
    for index, row in enumerate(daily):
        x = left + (plot_width * index / denominator)
        if len(daily) == 1:
            x = left + plot_width / 2
        revenue_y = top + plot_height * (
            1 - float(row["revenue"]) / revenue_scale
        )
        spend_y = top + plot_height * (
            1 - float(row["spend"]) / spend_scale
        )
        points.append(
            {
                "x": round(x, 2),
                "revenue_y": round(revenue_y, 2),
                "spend_y": round(spend_y, 2),
                "date": row["date"],
                "revenue": row["revenue"],
                "spend": row["spend"],
                "orders": row["orders"],
                "drr": row["drr"],
            }
        )
    revenue_line = " ".join(
        f"{point['x']},{point['revenue_y']}" for point in points
    )
    spend_line = " ".join(
        f"{point['x']},{point['spend_y']}" for point in points
    )
    baseline = height - bottom
    revenue_area = ""
    if points:
        revenue_area = (
            f"M {points[0]['x']} {baseline} "
            + " ".join(
                f"L {point['x']} {point['revenue_y']}" for point in points
            )
            + f" L {points[-1]['x']} {baseline} Z"
        )
    tick_indexes: list[int] = []
    if points:
        tick_count = min(7, len(points))
        tick_indexes = sorted(
            {
                round(index * (len(points) - 1) / max(tick_count - 1, 1))
                for index in range(tick_count)
            }
        )
    return {
        "width": width,
        "height": height,
        "baseline": baseline,
        "points": points,
        "revenue_line": revenue_line,
        "spend_line": spend_line,
        "revenue_area": revenue_area,
        "ticks": [points[index] for index in tick_indexes],
        "revenue_max": revenue_max,
        "spend_max": spend_max,
    }


def _decorate_rankings(
    rows: list[dict[str, Any]],
    totals: dict[str, Any],
    *,
    add_zones: bool = False,
) -> list[dict[str, Any]]:
    rows.sort(
        key=lambda row: (
            float(row["revenue"]),
            int(row["orders"]),
            -float(row["spend"]),
        ),
        reverse=True,
    )
    maximum = max(
        (
            max(float(row["revenue"]), float(row["spend"]))
            for row in rows
        ),
        default=1,
    )
    for index, row in enumerate(rows):
        row["rank"] = index + 1
        row["bar_revenue"] = 100 * float(row["revenue"]) / maximum
        row["bar_spend"] = 100 * float(row["spend"]) / maximum
        row["revenue_share"] = _ratio(row["revenue"], totals["revenue"])
        row["spend_share"] = _ratio(row["spend"], totals["spend"])
        if add_zones:
            row["zones"] = _performance_zones(row["label"], row)
    return rows


def _donut_background(rows: list[dict[str, Any]]) -> str:
    for index, row in enumerate(rows):
        row["color"] = CHART_COLORS[index % len(CHART_COLORS)]
    if not rows or not sum(float(row["spend"]) for row in rows):
        return "conic-gradient(var(--bp-surface-elevated) 0deg 360deg)"
    cursor = 0.0
    segments = []
    for row in rows:
        color = row["color"]
        end = cursor + 360 * float(row["spend_share"])
        segments.append(f"{color} {cursor:.2f}deg {end:.2f}deg")
        cursor = end
    return f"conic-gradient({', '.join(segments)})"


def _query_rows(
    session: Session,
    account_id: int,
    begin: date,
    end: date,
) -> list[Any]:
    statement = (
        select(
            DailyStat.stat_date,
            DailyStat.nm_id,
            DailyStat.advert_id,
            DailyStat.views,
            DailyStat.clicks,
            DailyStat.spend,
            DailyStat.atbs,
            DailyStat.orders,
            DailyStat.canceled,
            DailyStat.shks,
            DailyStat.revenue,
            Product.name.label("product_name"),
            Product.subject_name,
            Product.report_group,
            Campaign.name.label("campaign_name"),
        )
        .outerjoin(
            Product,
            (Product.account_id == DailyStat.account_id)
            & (Product.nm_id == DailyStat.nm_id),
        )
        .outerjoin(
            Campaign,
            (Campaign.account_id == DailyStat.account_id)
            & (Campaign.advert_id == DailyStat.advert_id),
        )
        .where(
            DailyStat.account_id == account_id,
            DailyStat.stat_date >= begin,
            DailyStat.stat_date <= end,
        )
        .order_by(DailyStat.stat_date)
    )
    return list(session.execute(statement).all())


def _filtered_rows(
    rows: list[Any],
    group: str,
    subject: str,
) -> list[tuple[Any, str, str]]:
    selected_group = _dimension_key(group)
    selected_subject = _dimension_key(subject)
    result = []
    for row in rows:
        report_group = _display_label(row.report_group, "Без категории")
        subject_name = _display_label(row.subject_name, report_group)
        if selected_group and _dimension_key(report_group) != selected_group:
            continue
        if (
            selected_subject
            and _dimension_key(subject_name) != selected_subject
        ):
            continue
        result.append((row, report_group, subject_name))
    return result


def available_dimensions(
    session: Session,
    account_id: int,
) -> tuple[list[str], list[str]]:
    products = session.execute(
        select(Product.report_group, Product.subject_name).where(
            Product.account_id == account_id
        )
    ).all()
    groups: dict[str, str] = {}
    subjects: dict[str, str] = {}
    for report_group_raw, subject_raw in products:
        report_group = _display_label(report_group_raw, "Без категории")
        subject = _display_label(subject_raw, report_group)
        groups.setdefault(_dimension_key(report_group), report_group)
        subjects.setdefault(_dimension_key(subject), subject)
    return (
        sorted(groups.values(), key=str.casefold),
        sorted(subjects.values(), key=str.casefold),
    )


def build_analytics_report(
    session: Session,
    account_id: int,
    begin: date,
    end: date,
    *,
    group: str = "",
    subject: str = "",
) -> dict[str, Any]:
    raw_rows = _query_rows(session, account_id, begin, end)
    rows = _filtered_rows(raw_rows, group, subject)
    totals_raw = _empty_metrics()
    daily_raw: dict[date, dict[str, Any]] = defaultdict(_empty_metrics)
    groups_raw: dict[str, dict[str, Any]] = defaultdict(_empty_metrics)
    subjects_raw: dict[str, dict[str, Any]] = defaultdict(_empty_metrics)
    products_raw: dict[int, dict[str, Any]] = {}
    campaigns_raw: dict[int, dict[str, Any]] = {}
    product_ids: set[int] = set()
    campaign_ids: set[int] = set()

    for row, report_group, subject_name in rows:
        _add_metrics(totals_raw, row)
        _add_metrics(daily_raw[row.stat_date], row)
        group_metrics = groups_raw[report_group]
        group_metrics["label"] = report_group
        _add_metrics(group_metrics, row)
        subject_metrics = subjects_raw[subject_name]
        subject_metrics["label"] = subject_name
        _add_metrics(subject_metrics, row)

        product_ids.add(int(row.nm_id))
        product_metrics = products_raw.setdefault(
            int(row.nm_id),
            {
                **_empty_metrics(),
                "nm_id": int(row.nm_id),
                "name": row.product_name or f"Артикул {row.nm_id}",
                "group": report_group,
                "subject": subject_name,
            },
        )
        _add_metrics(product_metrics, row)

        campaign_ids.add(int(row.advert_id))
        campaign_metrics = campaigns_raw.setdefault(
            int(row.advert_id),
            {
                **_empty_metrics(),
                "advert_id": int(row.advert_id),
                "name": row.campaign_name or f"Кампания {row.advert_id}",
            },
        )
        _add_metrics(campaign_metrics, row)

    totals = _complete_metrics(totals_raw)
    totals["products_count"] = len(product_ids)
    totals["campaigns_count"] = len(campaign_ids)
    daily = [
        {"date": stat_date, **_complete_metrics(metrics)}
        for stat_date, metrics in sorted(daily_raw.items())
    ]
    group_rows = [
        _complete_metrics(metrics) for metrics in groups_raw.values()
    ]
    subject_rows = [
        _complete_metrics(metrics) for metrics in subjects_raw.values()
    ]
    product_rows = [
        _complete_metrics(metrics) for metrics in products_raw.values()
    ]
    campaign_rows = [
        _complete_metrics(metrics) for metrics in campaigns_raw.values()
    ]
    _decorate_rankings(group_rows, totals, add_zones=True)
    _decorate_rankings(subject_rows, totals)
    _decorate_rankings(product_rows, totals)
    _decorate_rankings(campaign_rows, totals)

    period_days = (end - begin).days + 1
    previous_end = begin - timedelta(days=1)
    previous_begin = previous_end - timedelta(days=period_days - 1)
    previous_raw = _empty_metrics()
    previous_rows = _filtered_rows(
        _query_rows(session, account_id, previous_begin, previous_end),
        group,
        subject,
    )
    for row, _report_group, _subject_name in previous_rows:
        _add_metrics(previous_raw, row)
    previous = _complete_metrics(previous_raw)
    deltas = {
        key: _delta(totals[key], previous[key])
        for key in ("spend", "revenue", "orders", "ctr", "cpc", "drr")
    }

    return {
        "begin": begin,
        "end": end,
        "previous_begin": previous_begin,
        "previous_end": previous_end,
        "totals": totals,
        "previous": previous,
        "deltas": deltas,
        "trend": daily,
        "chart": _build_line_chart(daily),
        "groups": group_rows,
        "subjects": subject_rows[:10],
        "funnel_categories": subject_rows,
        "products": product_rows[:12],
        "campaigns": campaign_rows[:10],
        "donut_background": _donut_background(group_rows),
        "row_count": len(rows),
    }
