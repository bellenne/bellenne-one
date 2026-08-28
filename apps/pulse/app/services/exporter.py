from __future__ import annotations

import io
from calendar import monthrange
from datetime import date, timedelta

import xlsxwriter
from sqlalchemy.orm import Session
from xlsxwriter.utility import xl_rowcol_to_cell

from app.models import Marketplace
from app.services.metrics import METRIC_LABELS, aggregate_rows, decimal
from app.services.planning import (
    daily_plan,
    load_plan,
    month_start,
    plan_for_range,
    plan_monthly_values,
    rolling_six_week_window,
)
from app.services.reporting import available_groups, report_rows


BASE_METRICS = (
    "ordered_units",
    "ordered_amount",
    "average_order_value",
    "buyout_units",
    "buyout_amount",
    "ad_spend",
    "ad_bonus_spend",
    "drr_buyouts_total",
    "drr_orders_total",
    "drr_buyouts_cash",
    "drr_orders_cash",
    "gross_profit",
)

OZON_METRICS = (
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

DERIVED_METRICS = {
    "average_order_value",
    "drr_buyouts_total",
    "drr_orders_total",
    "drr_buyouts_cash",
    "drr_orders_cash",
}


def _sum_values(rows) -> dict[str, object]:
    return aggregate_rows(rows)


def _is_percent(metric_key: str) -> bool:
    return metric_key.startswith("drr_")


def _is_money(metric_key: str) -> bool:
    return metric_key in {
        "ordered_amount",
        "average_order_value",
        "buyout_amount",
        "net_revenue",
        "ad_spend",
        "ad_bonus_spend",
        "gross_profit",
    }


def _formula_for_derived(metric_key: str, row_map: dict[str, int], col: int) -> str | None:
    cell = lambda key: xl_rowcol_to_cell(row_map[key], col)
    if metric_key == "average_order_value":
        return f'=IFERROR({cell("ordered_amount")}/{cell("ordered_units")},0)'
    if metric_key == "drr_buyouts_total":
        return f'=IFERROR(({cell("ad_spend")}+{cell("ad_bonus_spend")})/{cell("buyout_amount")},0)'
    if metric_key == "drr_orders_total":
        return f'=IFERROR(({cell("ad_spend")}+{cell("ad_bonus_spend")})/{cell("ordered_amount")},0)'
    if metric_key == "drr_buyouts_cash":
        return f'=IFERROR({cell("ad_spend")}/{cell("buyout_amount")},0)'
    if metric_key == "drr_orders_cash":
        return f'=IFERROR({cell("ad_spend")}/{cell("ordered_amount")},0)'
    return None


def build_report_workbook(
    session: Session,
    user_id: int,
    period: date,
    account_id: int | None = None,
) -> bytes:
    period = month_start(period)
    window_start, window_end = rolling_six_week_window(period)
    groups = available_groups(session, user_id)

    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    workbook.set_properties(
        {
            "title": f"BellennePulse — Переработка тест — {period:%Y-%m}",
            "subject": "Автоматизированный отчёт WB и Ozon",
            "author": "BellennePulse",
            "comments": "План валовой прибыли заполняется пользователем; факт поступает из внешнего сервиса и остаётся пустым до загрузки.",
        }
    )
    sheet = workbook.add_worksheet("Переработка тест")
    sheet.hide_gridlines(2)
    sheet.freeze_panes(2, 1)
    sheet.set_zoom(55)
    sheet.set_landscape()
    sheet.fit_to_pages(1, 0)
    sheet.repeat_columns(0, 0)
    sheet.set_margins(0.2, 0.2, 0.35, 0.35)

    colors = {
        "blue": "#4A86E8",
        "light_blue": "#C9DAF8",
        "plan": "#D9EAD3",
        "fact": "#FFF2CC",
        "percent": "#E7E6E6",
        "white": "#FFFFFF",
        "text": "#111111",
        "purple": "#9900FF",
        "orange": "#B45F06",
        "green": "#38761D",
        "red": "#CC0000",
    }
    border = {"border": 1, "border_color": "#666666"}
    title_format = workbook.add_format(
        {**border, "bold": True, "font_size": 18, "font_color": colors["white"], "bg_color": colors["purple"], "align": "center", "valign": "vcenter", "text_wrap": True}
    )
    section_formats = [
        workbook.add_format({**border, "bold": True, "font_size": 14, "font_color": colors["white"], "bg_color": colors[color], "align": "center"})
        for color in ("orange", "green", "red")
    ]
    marketplace_format = workbook.add_format({**border, "bold": True, "font_size": 14, "font_color": colors["white"], "bg_color": colors["blue"], "align": "center"})
    label_format = workbook.add_format({**border, "bold": True, "bg_color": colors["light_blue"]})
    label_alt_format = workbook.add_format({**border, "bold": True, "bg_color": colors["white"]})
    plan_header = workbook.add_format({**border, "bold": True, "bg_color": colors["plan"], "align": "center", "valign": "vcenter", "text_wrap": True})
    fact_header = workbook.add_format({**border, "bold": True, "bg_color": colors["fact"], "align": "center", "valign": "vcenter", "text_wrap": True})
    percent_header = workbook.add_format({**border, "bold": True, "bg_color": colors["percent"], "align": "center", "valign": "vcenter", "text_wrap": True})
    day_header = workbook.add_format({**border, "bold": True, "font_color": colors["white"], "bg_color": colors["blue"], "align": "center", "num_format": "dd.mm.yyyy"})
    week_header = workbook.add_format({**border, "bold": True, "font_color": colors["white"], "bg_color": colors["blue"], "align": "center"})
    plan_number = workbook.add_format({**border, "bold": True, "bg_color": colors["plan"], "align": "right", "num_format": "#,##0"})
    plan_money = workbook.add_format({**border, "bold": True, "bg_color": colors["plan"], "align": "right", "num_format": "#,##0 [$₽-ru-RU]"})
    plan_percent = workbook.add_format({**border, "bold": True, "bg_color": colors["plan"], "align": "right", "num_format": "0.00%"})
    fact_number = workbook.add_format({**border, "bold": True, "bg_color": colors["fact"], "align": "right", "num_format": "#,##0"})
    fact_money = workbook.add_format({**border, "bold": True, "bg_color": colors["fact"], "align": "right", "num_format": "#,##0 [$₽-ru-RU]"})
    fact_percent = workbook.add_format({**border, "bold": True, "bg_color": colors["fact"], "align": "right", "num_format": "0.00%"})
    compare_number = workbook.add_format({**border, "bold": True, "bg_color": colors["percent"], "align": "right", "num_format": "0.00%"})
    blank_format = workbook.add_format({**border, "bg_color": colors["percent"]})
    spacer_format = workbook.add_format({"bg_color": colors["blue"]})

    sheet.set_column(0, 0, 36)
    sheet.set_column(1, 3, 15)

    metric_blocks: list[dict[str, object]] = []
    tall_rows: set[int] = set()
    row = 0
    for market_index, marketplace in enumerate((Marketplace.WB.value, Marketplace.OZON.value)):
        plan = load_plan(session, user_id, period, marketplace)
        monthly_plan = plan_monthly_values(plan)
        market_rows = report_rows(
            session,
            user_id,
            period,
            period.replace(day=monthrange(period.year, period.month)[1]),
            marketplace,
            account_id,
            totals_only=True,
        )
        monthly_actual = _sum_values(market_rows)
        metrics = BASE_METRICS if marketplace == Marketplace.WB.value else OZON_METRICS
        title = "Wildberries\n(Сводник)" if marketplace == Marketplace.WB.value else "Ozon"
        start_row = row
        tall_rows.add(start_row)
        sheet.merge_range(row, 0, row + 1, 0, title, title_format if marketplace == Marketplace.WB.value else marketplace_format)
        sheet.write(row + 1, 1, "План Месяц", plan_header)
        sheet.write(row + 1, 2, "Факт Месяц", fact_header)
        sheet.write(row + 1, 3, "% Выпол. плана", percent_header)
        row += 2
        total_row_map: dict[str, int] = {}
        for metric_index, metric_key in enumerate(metrics):
            total_row_map[metric_key] = row
            sheet.write(row, 0, METRIC_LABELS[metric_key], label_format if metric_index % 2 == 0 else label_alt_format)
            row += 1
        metric_blocks.append(
            {
                "marketplace": marketplace,
                "group_id": None,
                "row_map": total_row_map,
                "plan_obj": plan,
                "plan": monthly_plan,
                "actual": monthly_actual,
                "metrics": metrics,
            }
        )
        for group_index, group in enumerate(groups):
            group_rows = report_rows(
                session,
                user_id,
                period,
                period.replace(day=monthrange(period.year, period.month)[1]),
                marketplace,
                account_id,
                group.id,
                totals_only=False,
            )
            if not group_rows and plan is None:
                continue
            group_plan = plan_monthly_values(plan, group.id)
            group_actual = _sum_values(group_rows)
            section_format = section_formats[group_index % len(section_formats)]
            sheet.write(row, 0, f"{marketplace} {group.name}", section_format)
            for blank_col in range(1, 4):
                sheet.write_blank(row, blank_col, None, section_format)
            row += 1
            group_row_map: dict[str, int] = {}
            for metric_index, metric_key in enumerate(metrics):
                group_row_map[metric_key] = row
                sheet.write(row, 0, METRIC_LABELS[metric_key], label_format if metric_index % 2 == 0 else label_alt_format)
                row += 1
            metric_blocks.append(
                {
                    "marketplace": marketplace,
                    "group_id": group.id,
                    "row_map": group_row_map,
                    "plan_obj": plan,
                    "plan": group_plan,
                    "actual": group_actual,
                    "metrics": metrics,
                }
            )
        if market_index == 0:
            row += 1

    max_row = row
    for current_row in range(max_row):
        sheet.set_row(current_row, 26 if current_row in tall_rows else 20)

    def value_format(metric_key: str, kind: str):
        if kind == "compare":
            return compare_number
        if _is_percent(metric_key):
            return plan_percent if kind == "plan" else fact_percent
        if _is_money(metric_key):
            return plan_money if kind == "plan" else fact_money
        return plan_number if kind == "plan" else fact_number

    def write_block_values(block: dict[str, object], plan_col: int, fact_col: int, compare_col: int, plan_values: dict[str, object], fact_values: dict[str, object]):
        row_map: dict[str, int] = block["row_map"]
        for metric_key in block["metrics"]:
            target_row = row_map[metric_key]
            if metric_key == "gross_profit":
                sheet.write_number(
                    target_row,
                    plan_col,
                    float(decimal(plan_values.get(metric_key))),
                    value_format(metric_key, "plan"),
                )
                sheet.write_blank(target_row, fact_col, None, value_format(metric_key, "fact"))
                sheet.write_blank(target_row, compare_col, None, blank_format)
                continue
            plan_formula = _formula_for_derived(metric_key, row_map, plan_col)
            fact_formula = _formula_for_derived(metric_key, row_map, fact_col)
            if plan_formula:
                sheet.write_formula(target_row, plan_col, plan_formula, value_format(metric_key, "plan"), float(decimal(plan_values.get(metric_key))))
            else:
                sheet.write_number(target_row, plan_col, float(decimal(plan_values.get(metric_key))), value_format(metric_key, "plan"))
            if fact_formula:
                sheet.write_formula(target_row, fact_col, fact_formula, value_format(metric_key, "fact"), float(decimal(fact_values.get(metric_key))))
            else:
                sheet.write_number(target_row, fact_col, float(decimal(fact_values.get(metric_key))), value_format(metric_key, "fact"))
            plan_cell = xl_rowcol_to_cell(target_row, plan_col)
            fact_cell = xl_rowcol_to_cell(target_row, fact_col)
            sheet.write_formula(target_row, compare_col, f"=IFERROR({fact_cell}/{plan_cell},0)", compare_number, float(decimal(fact_values.get(metric_key))) / float(decimal(plan_values.get(metric_key))) if decimal(plan_values.get(metric_key)) else 0)

    def write_rollup_values(
        block: dict[str, object],
        plan_col: int,
        fact_col: int,
        compare_col: int,
        plan_values: dict[str, object],
        fact_values: dict[str, object],
        source_columns: list[tuple[int, int]],
    ) -> None:
        """Write auditable rollups that sum the visible daily cells."""
        row_map: dict[str, int] = block["row_map"]
        for metric_key in block["metrics"]:
            target_row = row_map[metric_key]
            if metric_key == "gross_profit":
                plan_sources = ",".join(xl_rowcol_to_cell(target_row, cols[0]) for cols in source_columns)
                sheet.write_formula(
                    target_row,
                    plan_col,
                    f"=SUM({plan_sources})",
                    value_format(metric_key, "plan"),
                    float(decimal(plan_values.get(metric_key))),
                )
                sheet.write_blank(target_row, fact_col, None, value_format(metric_key, "fact"))
                sheet.write_blank(target_row, compare_col, None, blank_format)
                continue

            if metric_key in DERIVED_METRICS:
                plan_formula = _formula_for_derived(metric_key, row_map, plan_col)
                fact_formula = _formula_for_derived(metric_key, row_map, fact_col)
            else:
                plan_sources = ",".join(xl_rowcol_to_cell(target_row, cols[0]) for cols in source_columns)
                fact_sources = ",".join(xl_rowcol_to_cell(target_row, cols[1]) for cols in source_columns)
                plan_formula = f"=SUM({plan_sources})"
                fact_formula = f"=SUM({fact_sources})"

            sheet.write_formula(
                target_row,
                plan_col,
                plan_formula,
                value_format(metric_key, "plan"),
                float(decimal(plan_values.get(metric_key))),
            )
            sheet.write_formula(
                target_row,
                fact_col,
                fact_formula,
                value_format(metric_key, "fact"),
                float(decimal(fact_values.get(metric_key))),
            )
            plan_cell = xl_rowcol_to_cell(target_row, plan_col)
            fact_cell = xl_rowcol_to_cell(target_row, fact_col)
            cached_compare = (
                float(decimal(fact_values.get(metric_key))) / float(decimal(plan_values.get(metric_key)))
                if decimal(plan_values.get(metric_key))
                else 0
            )
            sheet.write_formula(
                target_row,
                compare_col,
                f"=IFERROR({fact_cell}/{plan_cell},0)",
                compare_number,
                cached_compare,
            )

    for block in metric_blocks:
        write_block_values(block, 1, 2, 3, block["plan"], block["actual"])

    sheet.set_column(4, 4, 5, spacer_format)
    current_col = 5
    current_date = window_start
    day_columns: dict[date, tuple[int, int]] = {}
    daily_values: dict[tuple[int, date], dict[str, object]] = {}
    for week_index in range(6):
        week_day_cols: list[tuple[int, int]] = []
        week_start = current_date
        for day_index in range(7):
            plan_col, fact_col, compare_col, dynamic_col = current_col, current_col + 1, current_col + 2, current_col + 3
            sheet.set_column(plan_col, dynamic_col, 14)
            sheet.set_column(dynamic_col + 1, dynamic_col + 1, 5, spacer_format)
            sheet.merge_range(0, plan_col, 0, dynamic_col, current_date.strftime("%d.%m.%Y"), day_header)
            sheet.write(1, plan_col, "Дневной план", plan_header)
            sheet.write(1, fact_col, "Факт", fact_header)
            sheet.write(1, compare_col, "% Выполнения", percent_header)
            sheet.write(1, dynamic_col, "% Динамика", percent_header)
            for block_index, block in enumerate(metric_blocks):
                marketplace = block["marketplace"]
                group_id = block["group_id"]
                month_values = plan_monthly_values(block["plan_obj"], group_id)
                day_plan_values = daily_plan(month_values, period, current_date)
                rows = report_rows(session, user_id, current_date, current_date, marketplace, account_id, group_id, totals_only=group_id is None)
                day_actual_values = _sum_values(rows)
                daily_values[(block_index, current_date)] = day_actual_values
                write_block_values(block, plan_col, fact_col, compare_col, day_plan_values, day_actual_values)
                row_map: dict[str, int] = block["row_map"]
                for metric_key in block["metrics"]:
                    target_row = row_map[metric_key]
                    previous_columns = day_columns.get(current_date - timedelta(days=1))
                    if metric_key == "gross_profit" or previous_columns is None:
                        sheet.write_blank(target_row, dynamic_col, None, blank_format)
                    else:
                        current_cell = xl_rowcol_to_cell(target_row, fact_col)
                        previous_cell = xl_rowcol_to_cell(target_row, previous_columns[1])
                        current_value = decimal(day_actual_values.get(metric_key))
                        previous_day = current_date - timedelta(days=1)
                        previous_value = decimal(daily_values[(block_index, previous_day)].get(metric_key))
                        cached = float((current_value - previous_value) / previous_value) if previous_value else 0
                        sheet.write_formula(target_row, dynamic_col, f"=IFERROR(({current_cell}-{previous_cell})/{previous_cell},0)", compare_number, cached)
            week_day_cols.append((plan_col, fact_col))
            day_columns[current_date] = (plan_col, fact_col)
            current_col += 5
            current_date += timedelta(days=1)

        week_plan_col, week_fact_col, week_compare_col = current_col, current_col + 1, current_col + 2
        sheet.set_column(week_plan_col, week_compare_col, 15)
        if week_index < 5:
            sheet.set_column(week_compare_col + 1, week_compare_col + 1, 5, spacer_format)
        sheet.merge_range(0, week_plan_col, 0, week_compare_col, f"Неделя-{week_index + 1}", week_header)
        sheet.write(1, week_plan_col, "План неделя", plan_header)
        sheet.write(1, week_fact_col, "Факт неделя", fact_header)
        sheet.write(1, week_compare_col, "% Выпол. плана", percent_header)
        week_end = week_start + timedelta(days=6)
        for block in metric_blocks:
            marketplace = block["marketplace"]
            group_id = block["group_id"]
            month_values = plan_monthly_values(block["plan_obj"], group_id)
            week_plan_values = plan_for_range(month_values, period, week_start, week_end)
            rows = report_rows(session, user_id, week_start, week_end, marketplace, account_id, group_id, totals_only=group_id is None)
            week_actual_values = _sum_values(rows)
            write_rollup_values(
                block,
                week_plan_col,
                week_fact_col,
                week_compare_col,
                week_plan_values,
                week_actual_values,
                week_day_cols,
            )
        current_col += 4 if week_index < 5 else 3

    month_source_columns = [
        columns for target_date, columns in day_columns.items() if target_date.year == period.year and target_date.month == period.month
    ]
    for block in metric_blocks:
        write_rollup_values(
            block,
            1,
            2,
            3,
            block["plan"],
            block["actual"],
            month_source_columns,
        )

    last_col = current_col - 1
    sheet.print_area(0, 0, max_row - 1, last_col)
    workbook.close()
    output.seek(0)
    return output.read()
