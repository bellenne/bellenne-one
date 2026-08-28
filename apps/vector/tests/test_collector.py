from datetime import date

import pytest
from sqlalchemy import func, select

from app.models import Account, Campaign, DailyStat, Product, SyncRun
from app.services.collector import Collector, normalize_fullstats
from app.services.token_vault import TokenVault


def test_normalize_fullstats_aggregates_app_types(fullstats_payload):
    rows = normalize_fullstats(fullstats_payload)

    assert len(rows) == 1
    row = rows[0]
    assert row["advert_id"] == 28000001
    assert row["nm_id"] == 699712395
    assert row["views"] == 1500
    assert row["clicks"] == 50
    assert float(row["spend"]) == 500
    assert row["atbs"] == 10
    assert row["orders"] == 3
    assert row["canceled"] == 1
    assert float(row["revenue"]) == 6000


def test_existing_automatic_groups_are_normalized(
    database,
    settings,
    fake_client_factory,
):
    _engine, session_factory = database
    vault = TokenVault(settings.data_dir)
    with session_factory() as session:
        account = Account(
            name="Тест",
            encrypted_token=vault.encrypt("valid-token"),
        )
        session.add(account)
        session.flush()
        session.add_all(
            [
                Product(
                    account_id=account.id,
                    nm_id=1,
                    name="Флизелиновые фотообои на стену",
                    report_group="Без категории",
                    group_is_manual=False,
                ),
                Product(
                    account_id=account.id,
                    nm_id=2,
                    name="Флизелиновые фотообои на стену",
                    report_group="Настенная графика",
                    group_is_manual=True,
                ),
            ]
        )
        session.commit()

    collector = Collector(session_factory, vault, fake_client_factory)
    assert collector.normalize_product_groups() == 1

    with session_factory() as session:
        automatic = session.scalar(
            select(Product).where(Product.nm_id == 1)
        )
        manual = session.scalar(select(Product).where(Product.nm_id == 2))
        assert automatic.report_group == "Фотообои"
        assert manual.report_group == "Настенная графика"


@pytest.mark.asyncio
async def test_collector_upserts_idempotently(
    database,
    settings,
    fake_client_factory,
):
    _engine, session_factory = database
    vault = TokenVault(settings.data_dir)
    with session_factory() as session:
        account = Account(
            name="Тест",
            encrypted_token=vault.encrypt("valid-token"),
            token_hint="vali••••oken",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    collector = Collector(session_factory, vault, fake_client_factory)
    target_date = date(2026, 7, 27)
    await collector.collect(
        account_id,
        target_date,
        target_date,
        trigger="manual",
    )
    await collector.collect(
        account_id,
        target_date,
        target_date,
        trigger="manual",
    )

    with session_factory() as session:
        assert session.scalar(select(func.count(DailyStat.id))) == 1
        stat = session.scalar(select(DailyStat))
        assert stat.views == 1500
        assert stat.clicks == 50
        assert float(stat.spend) == 500
        assert stat.atbs == 10
        assert stat.orders == 3
        assert float(stat.revenue) == 6000

        product = session.scalar(select(Product))
        assert product.name == "Фотообои Горы"
        assert product.subject_name == "Фотообои"
        assert product.report_group == "Фотообои"

        campaign = session.scalar(select(Campaign))
        assert campaign.name == "Фотообои · поиск"
        assert campaign.status == 9

        runs = session.scalars(
            select(SyncRun).order_by(SyncRun.id)
        ).all()
        assert [run.status for run in runs] == ["success", "success"]
        assert all(run.records_upserted == 1 for run in runs)
