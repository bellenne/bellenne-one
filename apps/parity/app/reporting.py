"""Shared comparison semantics for charts, table highlights and Excel export."""
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

import xlsxwriter

from .domain import PRICE_TYPES, difference, now


def current_pair(row, key):
    left, right = row["wb_price"], row["ozon_price"]
    comparison = row["comparisons"].get(key)
    if (row["status"] not in ("matched", "mismatch") or not comparison
            or comparison.status not in ("matched", "mismatch") or not left or not right
            or not left.currency or left.currency != right.currency):
        return None
    wb, ozon = getattr(left, key), getattr(right, key)
    if wb is None or ozon is None:
        return None
    signed, percent = difference(wb, ozon)
    return {"wb": wb, "ozon": ozon, "signed": signed, "percent": percent,
            "currency": left.currency, "direction": "wb_cheaper" if signed > 0 else "ozon_cheaper" if signed < 0 else "equal"}


def comparison_summary(rows, key):
    counts = dict(wb_cheaper=0, ozon_cheaper=0, equal=0, unavailable=0)
    ranked = []
    for row in rows:
        pair = current_pair(row, key)
        row["focus_pair"] = pair
        counts[pair["direction"] if pair else "unavailable"] += 1
        if pair:
            item = row["wb"] or row["ozon"]
            ranked.append({"id": row["mapping"].id, "article": item.seller_article or item.sku or str(row["mapping"].id),
                           **pair})
    # Chart uses RUB only: amounts in different currencies cannot share an axis.
    rub = sorted((p for p in ranked if p["currency"] == "RUB"), key=lambda p: (-abs(p["signed"]), p["id"]))
    top = rub[:8]
    maximum = max([max(p["wb"], p["ozon"]) for p in top] or [Decimal(1)]) or Decimal(1)
    return {"counts": counts, "total": len(rows), "compared": len(ranked), "rub_total": len(rub),
            "other_currency": len(ranked) - len(rub), "top": top, "maximum": maximum}


def excel_report(rows, policy, filters, labels, status_labels, title):
    output = BytesIO()
    with xlsxwriter.Workbook(output, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False}) as book:
        book.set_properties({"title": "BellenneParity · " + title, "author": "Bellenne"})
        heading = book.add_format({"bold": True, "text_wrap": True, "bottom": 1})
        money = book.add_format({"num_format": '#,##0.00;[Red]-#,##0.00'})
        percent = book.add_format({"num_format": '+0.0%;-0.0%;0.0%'})
        date = book.add_format({"num_format": 'dd.mm.yyyy hh:mm'})
        text = book.add_format({"num_format": '@', "valign": 'top'})
        wrap = book.add_format({"text_wrap": True, "valign": 'top'})
        zone = ZoneInfo(policy.timezone)

        def write(sheet, r, c, value, fmt=None):
            if value is None:
                sheet.write_blank(r, c, None, fmt)
            elif isinstance(value, datetime):
                sheet.write_datetime(r, c, value.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None), date)
            elif isinstance(value, (Decimal, int)):
                sheet.write_number(r, c, float(value), fmt)
            else:
                sheet.write_string(r, c, str(value), fmt or text)

        sheet = book.add_worksheet("Сравнение")
        headers = ["Артикул WB", "Артикул Ozon", "Название WB", "Название Ozon", "WB SKU", "Ozon SKU", "Ozon Product ID", "Статус", "Связь", "Валюта WB", "Валюта Ozon"]
        for key in PRICE_TYPES:
            headers += ["WB · " + labels[key], "Ozon · " + labels[key], labels[key] + " · Ozon − WB", labels[key] + " · разница к WB, %"]
        headers += ["Собрано WB", "Собрано Ozon", "Обновлено", "Сбор WB", "Сбор Ozon"]
        sheet.write_row(0, 0, headers, heading)
        sheet.set_row(0, 42)
        sheet.set_column(0, 1, 24, text)
        sheet.set_column(2, 3, 40, wrap)
        sheet.set_column(4, 6, 22, text)
        sheet.set_column(7, 10, 22)
        sheet.set_column(11, len(headers) - 1, 23)
        sheet.freeze_panes(1, 2)
        for index, row in enumerate(rows, 1):
            wb, ozon, left, right = row["wb"], row["ozon"], row["wb_price"], row["ozon_price"]
            values = [wb.seller_article if wb else None, ozon.seller_article if ozon else None,
                      wb.name if wb else None, ozon.name if ozon else None, wb.sku if wb else None,
                      ozon.sku if ozon else None, ozon.external_id if ozon else None,
                      status_labels.get(row["status"], row["status"]), "Ручная" if row["mapping"].mapping_type == "manual" else "Автоматическая",
                      left.currency if left else None, right.currency if right else None]
            for c, value in enumerate(values):
                write(sheet, index, c, value, wrap if c in (2, 3) else text)
            for offset, key in enumerate(PRICE_TYPES):
                pair = current_pair(row, key)
                values = [getattr(left, key) if left else None, getattr(right, key) if right else None,
                          pair["signed"] if pair else None, pair["percent"] / 100 if pair and pair["percent"] is not None else None]
                for c, value in enumerate(values, 11 + 4 * offset):
                    write(sheet, index, c, value, percent if (c-11) % 4 == 3 else money)
            for c, value in enumerate([left.captured_at if left else None, right.captured_at if right else None,
                                       row["updated_at"], status_labels.get(wb.collection_status, wb.collection_status) if wb else None,
                                       status_labels.get(ozon.collection_status, ozon.collection_status) if ozon else None], 23):
                write(sheet, index, c, value)
        sheet.autofilter(0, 0, len(rows), len(headers) - 1)

        summary = book.add_worksheet("О выгрузке")
        summary.set_column(0, 0, 32)
        summary.set_column(1, 1, 90, wrap)
        metadata = [("Раздел", title), ("Выгружено товаров", len(rows)), ("Время выгрузки", now()),
                    ("Часовой пояс", policy.timezone), ("Источник", "Витрина · WB Кошелёк / Ozon Карта" if policy.price_source == "storefront" else "Seller API"),
                    ("Контекст витрины", policy.context_version if policy.price_source == "storefront" else "—"),
                    ("Разница", "Ozon − WB. Положительная разница: WB дешевле. Процент рассчитан к цене WB."),
                    ("Недоступные данные", "Пустая ячейка — цена недоступна. Старые цены сохранены справочно; разница рассчитывается только по актуальным данным."),
                    ("Область выгрузки", "Все товары по применённым фильтрам, независимо от страницы каталога.")]
        metadata += [("Фильтр · " + key, value) for key, value in filters.items() if key not in ("page", "page_size") and value not in (None, "", False)]
        for r, (label, value) in enumerate(metadata):
            write(summary, r, 0, label, heading)
            write(summary, r, 1, value, wrap)
        summary.freeze_panes(1, 0)
    return output.getvalue()
