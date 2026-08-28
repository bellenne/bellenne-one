from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
import re

from sqlalchemy import select

from app.models import DailyMetric, MarketplaceProduct, MarketplaceAccount, ProductGroup, User
from app.services.exporter import build_report_workbook
from app.services.integrations.base import MetricPayload, ProductMetric, SyncPayload
from app.services.planning import save_plan
from app.services.reporting import report_rows
from app.services.sync import persist_payload, rebuild_user_totals
from tests.conftest import csrf_from


def test_seeded_metrics_never_fill_gross_profit(client):
    with client.app.state.session_factory() as session:
        rows = list(session.scalars(select(DailyMetric)))
        assert rows
        assert all(row.gross_profit is None for row in rows)


def test_xlsx_export_contains_only_target_sheet(authenticated_client):
    response = authenticated_client.get("/reports/export.xlsx?period=2026-08")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
        shared_strings = archive.read("xl/sharedStrings.xml").decode("utf-8")
        assert "Переработка тест" in workbook_xml
        assert "общая август" not in workbook_xml
        assert "Валовая прибыль" in shared_strings
        assert "Дневной план" in shared_strings
        assert "Неделя-1" in shared_strings
        sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "<f>SUM(" in sheet_xml
        assert "<f>IFERROR(" in sheet_xml
        assert not re.search(r'ref="A\d+:D\d+"', sheet_xml)


def test_xlsx_exports_gross_profit_plan_but_not_actual(authenticated_client):
    with authenticated_client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.username == "test-owner"))
        groups = list(session.scalars(select(ProductGroup).where(ProductGroup.user_id == user.id)))
        total_values = {
            "ordered_units": Decimal("0"),
            "ordered_amount": Decimal("0"),
            "buyout_units": Decimal("0"),
            "buyout_amount": Decimal("0"),
            "net_revenue": Decimal("0"),
            "ad_spend": Decimal("0"),
            "ad_bonus_spend": Decimal("0"),
            "gross_profit": Decimal("31000.25"),
        }
        allocations = {group.id: (Decimal("100") if index == 0 else Decimal("0")) for index, group in enumerate(groups)}
        save_plan(session, user.id, date(2030, 1, 1), "WB", "total", total_values, {}, allocations)
        workbook = build_report_workbook(session, user.id, date(2030, 1, 1))
    with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
        strings = archive.read("xl/sharedStrings.xml").decode("utf-8")
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        gross_index = re.search(r'<si><t>Валовая прибыль</t></si>', strings)
        assert gross_index is not None
        assert "31000.25" in sheet


def test_disabled_product_is_excluded_when_date_is_resynced(authenticated_client):
    target = date(2031, 1, 15)
    with authenticated_client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.username == "test-owner"))
        account = session.scalar(
            select(MarketplaceAccount).where(MarketplaceAccount.user_id == user.id, MarketplaceAccount.is_demo.is_(True))
        )
        assert account is not None
        payload = SyncPayload(
            total=MetricPayload(ordered_units=3, ordered_amount=Decimal("300")),
            products=[
                ProductMetric("toggle-a", "Товар A", "Toggle A", metrics=MetricPayload(ordered_units=1, ordered_amount=Decimal("100"))),
                ProductMetric("toggle-b", "Товар B", "Toggle B", metrics=MetricPayload(ordered_units=2, ordered_amount=Decimal("200"))),
            ],
        )
        persist_payload(session, account, target, payload)
        session.commit()
        disabled = session.scalar(select(MarketplaceProduct).where(MarketplaceProduct.account_id == account.id, MarketplaceProduct.external_id == "toggle-a"))
        disabled.is_active = False
        session.commit()
        persist_payload(session, account, target, payload)
        session.commit()
        total = session.scalar(select(DailyMetric).where(DailyMetric.account_id == account.id, DailyMetric.metric_date == target, DailyMetric.scope_key == "total"))
        group_rows = list(session.scalars(select(DailyMetric).where(DailyMetric.account_id == account.id, DailyMetric.metric_date == target, DailyMetric.scope_key != "total")))
        assert total.ordered_units == 2
        assert total.ordered_amount == Decimal("200")
        assert len(group_rows) == 1


def test_disabled_category_is_excluded_from_existing_totals(authenticated_client):
    target = date(2031, 2, 15)
    with authenticated_client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.username == "test-owner"))
        account = session.scalar(
            select(MarketplaceAccount).where(
                MarketplaceAccount.user_id == user.id,
                MarketplaceAccount.is_demo.is_(True),
            )
        )
        payload = SyncPayload(
            total=MetricPayload(ordered_units=3, ordered_amount=Decimal("300")),
            products=[
                ProductMetric("category-a", "Товар A", "Категория A", metrics=MetricPayload(ordered_units=1, ordered_amount=Decimal("100"))),
                ProductMetric("category-b", "Товар B", "Категория B", metrics=MetricPayload(ordered_units=2, ordered_amount=Decimal("200"))),
            ],
        )
        persist_payload(session, account, target, payload)
        session.commit()
        account_id = account.id
        group = session.scalar(
            select(ProductGroup).where(
                ProductGroup.user_id == user.id,
                ProductGroup.name == "Категория A",
            )
        )
        group_id = group.id

    page = authenticated_client.get("/accounts")
    response = authenticated_client.post(
        f"/product-groups/{group_id}/toggle",
        data={"csrf_token": csrf_from(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with authenticated_client.app.state.session_factory() as session:
        group = session.get(ProductGroup, group_id)
        total = session.scalar(
            select(DailyMetric).where(
                DailyMetric.metric_date == target,
                DailyMetric.scope_key == "total",
                DailyMetric.account_id == account_id,
            )
        )
        assert group.is_active is False
        assert total.ordered_units == 2
        assert total.ordered_amount == Decimal("200")
        assert report_rows(session, group.user_id, target, target, product_group_id=group_id) == []

    page = authenticated_client.get("/accounts")
    authenticated_client.post(
        f"/product-groups/{group_id}/toggle",
        data={"csrf_token": csrf_from(page.text)},
        follow_redirects=False,
    )


def test_unallocated_wb_advertising_survives_category_recalculation(authenticated_client):
    target = date(2031, 3, 15)
    with authenticated_client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.username == "test-owner"))
        account = session.scalar(
            select(MarketplaceAccount).where(
                MarketplaceAccount.user_id == user.id,
                MarketplaceAccount.is_demo.is_(True),
            )
        )
        payload = SyncPayload(
            total=MetricPayload(ad_spend=Decimal("100"), ad_bonus_spend=Decimal("40")),
            products=[
                ProductMetric(
                    "residual-a",
                    "Товар A",
                    "Остаток A",
                    metrics=MetricPayload(ad_spend=Decimal("40"), ad_bonus_spend=Decimal("10")),
                ),
                ProductMetric(
                    "residual-b",
                    "Товар B",
                    "Остаток B",
                    metrics=MetricPayload(ad_spend=Decimal("20"), ad_bonus_spend=Decimal("10")),
                ),
            ],
            authoritative_total_fields={"ad_spend", "ad_bonus_spend"},
        )
        persist_payload(session, account, target, payload)
        session.commit()

        group = session.scalar(
            select(ProductGroup).where(
                ProductGroup.user_id == user.id,
                ProductGroup.name == "Остаток A",
            )
        )
        total = session.scalar(
            select(DailyMetric).where(
                DailyMetric.account_id == account.id,
                DailyMetric.metric_date == target,
                DailyMetric.scope_key == "total",
            )
        )
        assert total.ad_spend == Decimal("100")
        assert total.ad_bonus_spend == Decimal("40")

        group.is_active = False
        rebuild_user_totals(session, user.id)
        session.commit()
        assert total.ad_spend == Decimal("60")
        assert total.ad_bonus_spend == Decimal("30")

        group.is_active = True
        rebuild_user_totals(session, user.id)
        session.commit()
        assert total.ad_spend == Decimal("100")
        assert total.ad_bonus_spend == Decimal("40")


def test_metrics_api_is_serializable(authenticated_client):
    response = authenticated_client.get("/api/metrics?date_from=2026-08-01&date_to=2026-08-25")
    assert response.status_code == 200
    payload = response.json()
    assert payload["series"]
    assert payload["summary"]["gross_profit"] is None
