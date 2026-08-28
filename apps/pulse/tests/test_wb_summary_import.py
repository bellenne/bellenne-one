from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from sqlalchemy import select

from app.config import Settings
from app.database import Base, build_engine, build_session_factory
from app.models import (
    DailyMetric,
    Marketplace,
    MarketplaceAccount,
    User,
    WBSummaryMetric,
)
from app.services.integrations.base import MetricPayload, SyncPayload
from app.services.sync import persist_payload, rebuild_user_totals
from app.services.wb_summary import import_wb_summary, parse_wb_summary_xlsx
from app import web as web_module
from tests.conftest import csrf_from


HEADERS = [
    "Год",
    "Месяц",
    "День",
    "Сумма заказов по розничным ценам с учётом согласованной скидки, руб.",
    "Заказано, шт.",
    "Количество заказов",
    "Сумма продаж по розничным ценам с учётом согласованной скидки, руб.",
    "Выкупили, шт.",
    "Коэффициент наценки продаж по оплатам",
    "Процент выкупа",
    "Оборачиваемость, дней",
    "К перечислению за товар, руб.",
]


def _summary_xlsx(rows: list[list[object]], headers: list[str] | None = None) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers or HEADERS)
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _report_row(
    metric_date: date,
    ordered_amount: object,
    ordered_units: object,
    buyout_amount: object,
    buyout_units: object,
    net_revenue: object,
) -> list[object]:
    return [
        metric_date.year,
        f"{metric_date.month:02d}",
        metric_date,
        ordered_amount,
        ordered_units,
        ordered_units,
        buyout_amount,
        buyout_units,
        0,
        0,
        0,
        net_revenue,
    ]


def test_wb_summary_parser_reads_daily_rows_and_skips_today():
    yesterday = date(2026, 8, 25)
    content = _summary_xlsx(
        [
            [2026, "08", None, 999999, 999, 999, 999999, 999, 0, 0, 0, 999999],
            _report_row(yesterday, 1557190, 288, 1013937, 184, 630469),
            _report_row(yesterday + timedelta(days=1), 101960, 19, 4669, 1, 2907),
        ]
    )

    result = parse_wb_summary_xlsx(content, yesterday)

    assert result.skipped_after_cutoff == 1
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.metric_date == yesterday
    assert row.ordered_units == 288
    assert row.ordered_amount == Decimal("1557190")
    assert row.buyout_units == 184
    assert row.buyout_amount == Decimal("1013937")
    assert row.net_revenue == Decimal("630469")


def test_wb_summary_parser_rejects_a_different_workbook():
    content = _summary_xlsx([], headers=["Дата", "Продажи"])

    try:
        parse_wb_summary_xlsx(content, date(2026, 8, 25))
    except ValueError as exc:
        assert "не похож" in str(exc)
    else:
        raise AssertionError("Parser accepted an unrelated workbook")


def test_wb_summary_survives_later_api_sync(tmp_path):
    engine = build_engine(
        Settings(
            database_url=f"sqlite:///{(tmp_path / 'summary.db').as_posix()}",
            scheduler_enabled=False,
            seed_demo_data=False,
            demo_mode=False,
        )
    )
    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)
    target_date = date(2026, 8, 25)
    with session_factory() as session:
        user = User(username="summary-owner", password_hash="unused")
        session.add(user)
        session.flush()
        account = MarketplaceAccount(
            user_id=user.id,
            name="WB",
            marketplace=Marketplace.WB.value,
            is_demo=False,
        )
        session.add(account)
        session.commit()

        import_wb_summary(
            session,
            account,
            _summary_xlsx(
                [_report_row(target_date, 1557190, 288, 1013937, 184, 630469)]
            ),
            "seller-summary.xlsx",
            target_date,
        )
        session.commit()

        api_payload = SyncPayload(
            total=MetricPayload(
                ordered_units=239,
                ordered_amount=Decimal("1288933.45"),
                buyout_units=184,
                buyout_amount=Decimal("1013937.18"),
                net_revenue=Decimal("644174.56"),
                ad_spend=Decimal("64936"),
                ad_bonus_spend=Decimal("64315"),
            ),
            products=[],
            authoritative_total_fields={"ad_spend", "ad_bonus_spend"},
        )
        persist_payload(session, account, target_date, api_payload)
        session.commit()

        summary = session.scalar(select(WBSummaryMetric))
        metric = session.scalar(
            select(DailyMetric).where(
                DailyMetric.account_id == account.id,
                DailyMetric.metric_date == target_date,
                DailyMetric.scope_key == "total",
            )
        )
        assert summary is not None
        assert metric.ordered_units == 288
        assert metric.ordered_amount == Decimal("1557190")
        assert metric.buyout_units == 184
        assert metric.buyout_amount == Decimal("1013937")
        assert metric.net_revenue == Decimal("630469")
        assert metric.ad_spend == Decimal("64936")
        assert metric.ad_bonus_spend == Decimal("64315")
        assert metric.source == "api+wb_summary"
        assert metric.source == "api+wb_summary"

        rebuild_user_totals(session, user.id)
        session.commit()
        session.refresh(metric)
        assert metric.ordered_units == 288
        assert metric.ordered_amount == Decimal("1557190")
        assert metric.buyout_units == 184
        assert metric.buyout_amount == Decimal("1013937")
        assert metric.net_revenue == Decimal("630469")
        assert metric.ad_spend == Decimal("64936")
        assert metric.ad_bonus_spend == Decimal("64315")


def test_wb_summary_upload_route_skips_today(authenticated_client):
    today = date.today()
    yesterday = today - timedelta(days=1)
    with authenticated_client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.username == "test-owner"))
        account = MarketplaceAccount(
            user_id=user.id,
            name="WB summary route",
            marketplace=Marketplace.WB.value,
            is_demo=False,
        )
        session.add(account)
        session.commit()
        account_id = account.id

    page = authenticated_client.get("/accounts")
    response = authenticated_client.post(
        f"/accounts/{account_id}/wb-summary",
        data={"csrf_token": csrf_from(page.text)},
        files={
            "summary_file": (
                "report.xlsx",
                _summary_xlsx(
                    [
                        _report_row(yesterday, 1557190, 288, 1013937, 184, 630469),
                        _report_row(today, 101960, 19, 4669, 1, 2907),
                    ]
                ),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with authenticated_client.app.state.session_factory() as session:
        summaries = list(
            session.scalars(
                select(WBSummaryMetric)
                .where(WBSummaryMetric.account_id == account_id)
                .order_by(WBSummaryMetric.metric_date)
            )
        )
        assert [item.metric_date for item in summaries] == [yesterday]


def test_manual_sync_rejects_today(authenticated_client, monkeypatch):
    calls: list[date] = []

    async def fake_sync_account(session, account, target_date, cipher, settings):
        calls.append(target_date)
        raise AssertionError("Today's sync must not run")

    with authenticated_client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.username == "test-owner"))
        account = MarketplaceAccount(
            user_id=user.id,
            name="WB no today",
            marketplace=Marketplace.WB.value,
            is_demo=False,
        )
        session.add(account)
        session.commit()
        account_id = account.id

    monkeypatch.setattr(web_module, "sync_account", fake_sync_account)
    page = authenticated_client.get("/accounts")
    response = authenticated_client.post(
        f"/accounts/{account_id}/sync",
        data={"csrf_token": csrf_from(page.text), "target_date": date.today().isoformat()},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert calls == []
