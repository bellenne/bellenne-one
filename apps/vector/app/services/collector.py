from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, Campaign, DailyStat, Product, SyncRun, utcnow
from app.services.grouping import canonical_report_group
from app.services.token_vault import TokenVault
from app.services.wildberries import WildberriesClient

ClientFactory = Callable[[str], WildberriesClient]


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _int(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(value)


def normalize_fullstats(
    payload: list[dict[str, Any]],
    product_metadata: dict[int, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    product_metadata = product_metadata or {}
    aggregate: dict[tuple[int, int, date], dict[str, Any]] = {}

    for advert in payload:
        advert_id = int(advert.get("advertId", advert.get("advert_id", 0)))
        for day_data in advert.get("days", []) or []:
            stat_date = date.fromisoformat(str(day_data["date"])[:10])
            found_nms = False
            for app_data in day_data.get("apps", []) or []:
                nms = app_data.get("nms") or app_data.get("nm") or []
                for nm_data in nms:
                    found_nms = True
                    nm_id = int(nm_data.get("nmId", nm_data.get("nm_id", 0)))
                    key = (advert_id, nm_id, stat_date)
                    item = aggregate.setdefault(
                        key,
                        {
                            "advert_id": advert_id,
                            "nm_id": nm_id,
                            "stat_date": stat_date,
                            "product_name": nm_data.get("name", ""),
                            "views": 0,
                            "clicks": 0,
                            "spend": Decimal("0"),
                            "atbs": 0,
                            "orders": 0,
                            "canceled": 0,
                            "shks": 0,
                            "revenue": Decimal("0"),
                        },
                    )
                    item["product_name"] = (
                        nm_data.get("name")
                        or item["product_name"]
                        or product_metadata.get(nm_id, {}).get("name", "")
                    )
                    _add_metrics(item, nm_data)

            if not found_nms:
                nm_id = 0
                key = (advert_id, nm_id, stat_date)
                item = aggregate.setdefault(
                    key,
                    {
                        "advert_id": advert_id,
                        "nm_id": 0,
                        "stat_date": stat_date,
                        "product_name": "Без детализации по артикулу",
                        "views": 0,
                        "clicks": 0,
                        "spend": Decimal("0"),
                        "atbs": 0,
                        "orders": 0,
                        "canceled": 0,
                        "shks": 0,
                        "revenue": Decimal("0"),
                    },
                )
                _add_metrics(item, day_data)

    return list(aggregate.values())


def _add_metrics(target: dict[str, Any], source: dict[str, Any]) -> None:
    target["views"] += _int(source.get("views"))
    target["clicks"] += _int(source.get("clicks"))
    target["spend"] += _decimal(
        source.get("sum", source.get("spend", 0))
    )
    target["atbs"] += _int(source.get("atbs"))
    target["orders"] += _int(source.get("orders"))
    target["canceled"] += _int(source.get("canceled"))
    target["shks"] += _int(source.get("shks"))
    target["revenue"] += _decimal(
        source.get("sum_price", source.get("revenue", 0))
    )


class Collector:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        vault: TokenVault,
        client_factory: ClientFactory,
    ) -> None:
        self.session_factory = session_factory
        self.vault = vault
        self.client_factory = client_factory
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def normalize_product_groups(self) -> int:
        changed = 0
        with self.session_factory() as session:
            products = session.scalars(select(Product)).all()
            for product in products:
                if product.group_is_manual:
                    continue
                normalized = canonical_report_group(
                    product.report_group,
                    product.subject_name,
                    product.name,
                )
                if normalized != product.report_group:
                    product.report_group = normalized
                    changed += 1
            session.commit()
        return changed

    async def collect(
        self,
        account_id: int,
        begin: date,
        end: date,
        *,
        trigger: str,
    ) -> int:
        if begin > end:
            raise ValueError("Дата начала не может быть позже даты окончания")

        async with self._locks[account_id]:
            run_id = self._create_run(account_id, begin, end, trigger)
            try:
                stats_count, upserted = await self._collect(
                    account_id, begin, end
                )
            except Exception as exc:
                self._finish_run(
                    run_id,
                    status="failed",
                    message=str(exc),
                )
                raise
            self._finish_run(
                run_id,
                status="success",
                records_received=stats_count,
                records_upserted=upserted,
                message="Синхронизация завершена",
            )
            return run_id

    def _create_run(
        self,
        account_id: int,
        begin: date,
        end: date,
        trigger: str,
    ) -> int:
        with self.session_factory() as session:
            run = SyncRun(
                account_id=account_id,
                requested_from=begin,
                requested_to=end,
                trigger=trigger,
                status="running",
            )
            session.add(run)
            session.commit()
            return run.id

    def _finish_run(
        self,
        run_id: int,
        *,
        status: str,
        records_received: int = 0,
        records_upserted: int = 0,
        message: str = "",
    ) -> None:
        with self.session_factory() as session:
            run = session.get(SyncRun, run_id)
            if run is None:
                return
            run.status = status
            run.records_received = records_received
            run.records_upserted = records_upserted
            run.message = message[:2000]
            run.finished_at = utcnow()
            session.commit()

    async def _collect(
        self,
        account_id: int,
        begin: date,
        end: date,
    ) -> tuple[int, int]:
        with self.session_factory() as session:
            account = session.get(Account, account_id)
            if account is None:
                raise ValueError("Кабинет не найден")
            token = self.vault.decrypt(account.encrypted_token)

        async with self.client_factory(token) as client:
            campaign_index = await client.list_campaigns()
            eligible = [
                item
                for item in campaign_index
                if item.get("status") in {7, 9, 11}
            ]
            campaign_ids = sorted(
                {int(item["advert_id"]) for item in eligible}
            )
            if not campaign_ids:
                return 0, 0

            details = await client.campaign_details(campaign_ids)
            product_metadata = self._upsert_metadata(
                account_id, eligible, details
            )

            received = 0
            upserted = 0
            async for payload in client.full_stats(
                campaign_ids, begin, end
            ):
                normalized = normalize_fullstats(payload, product_metadata)
                received += len(normalized)
                upserted += self._upsert_stats(
                    account_id, normalized, product_metadata
                )

        return received, upserted

    def _upsert_metadata(
        self,
        account_id: int,
        campaign_index: list[dict[str, Any]],
        details: list[dict[str, Any]],
    ) -> dict[int, dict[str, str]]:
        index_by_id = {
            int(item["advert_id"]): item for item in campaign_index
        }
        product_metadata: dict[int, dict[str, str]] = {}

        with self.session_factory() as session:
            for detail in details:
                advert_id = int(
                    detail.get("id", detail.get("advertId", 0))
                )
                settings = detail.get("settings") or {}
                index_item = index_by_id.get(advert_id, {})
                statement = sqlite_insert(Campaign).values(
                    account_id=account_id,
                    advert_id=advert_id,
                    name=settings.get("name", detail.get("name", "")),
                    status=detail.get("status", index_item.get("status")),
                    campaign_type=index_item.get("campaign_type"),
                    payment_type=settings.get("payment_type"),
                    last_seen_at=utcnow(),
                )
                statement = statement.on_conflict_do_update(
                    index_elements=["account_id", "advert_id"],
                    set_={
                        "name": statement.excluded.name,
                        "status": statement.excluded.status,
                        "campaign_type": statement.excluded.campaign_type,
                        "payment_type": statement.excluded.payment_type,
                        "last_seen_at": statement.excluded.last_seen_at,
                    },
                )
                session.execute(statement)

                for nm_settings in detail.get("nm_settings", []) or []:
                    nm_id = int(
                        nm_settings.get("nm_id", nm_settings.get("nmId", 0))
                    )
                    subject = nm_settings.get("subject") or {}
                    subject_name = str(subject.get("name") or "")
                    campaign_name = str(
                        settings.get("name", detail.get("name", ""))
                    )
                    existing = session.scalar(
                        select(Product).where(
                            Product.account_id == account_id,
                            Product.nm_id == nm_id,
                        )
                    )
                    if existing is None:
                        existing = Product(
                            account_id=account_id,
                            nm_id=nm_id,
                            name="",
                            subject_name=subject_name,
                            report_group=canonical_report_group(
                                subject_name,
                                campaign_name,
                            ),
                            group_is_manual=False,
                        )
                        session.add(existing)
                    else:
                        existing.subject_name = subject_name
                        if not existing.group_is_manual:
                            existing.report_group = canonical_report_group(
                                existing.report_group,
                                subject_name,
                                existing.name,
                                campaign_name,
                            )
                    product_metadata[nm_id] = {
                        "name": existing.name,
                        "subject_name": subject_name,
                        "report_group": existing.report_group,
                    }
            session.commit()

        return product_metadata

    def _upsert_stats(
        self,
        account_id: int,
        rows: list[dict[str, Any]],
        product_metadata: dict[int, dict[str, str]],
    ) -> int:
        with self.session_factory() as session:
            for row in rows:
                nm_id = int(row["nm_id"])
                product_name = str(row.get("product_name") or "")
                product = session.scalar(
                    select(Product).where(
                        Product.account_id == account_id,
                        Product.nm_id == nm_id,
                    )
                )
                if product is None:
                    metadata = product_metadata.get(nm_id, {})
                    group = metadata.get("report_group") or "Без категории"
                    product = Product(
                        account_id=account_id,
                        nm_id=nm_id,
                        name=product_name,
                        subject_name=metadata.get("subject_name", ""),
                        report_group=group,
                    )
                    session.add(product)
                elif product_name:
                    product.name = product_name
                    if not product.group_is_manual:
                        product.report_group = canonical_report_group(
                            product.report_group,
                            product.subject_name,
                            product_name,
                        )

                values = {
                    "account_id": account_id,
                    "advert_id": int(row["advert_id"]),
                    "nm_id": nm_id,
                    "stat_date": row["stat_date"],
                    "views": int(row["views"]),
                    "clicks": int(row["clicks"]),
                    "spend": row["spend"],
                    "atbs": int(row["atbs"]),
                    "orders": int(row["orders"]),
                    "canceled": int(row["canceled"]),
                    "shks": int(row["shks"]),
                    "revenue": row["revenue"],
                    "updated_at": utcnow(),
                }
                statement = sqlite_insert(DailyStat).values(**values)
                statement = statement.on_conflict_do_update(
                    index_elements=[
                        "account_id",
                        "advert_id",
                        "nm_id",
                        "stat_date",
                    ],
                    set_={
                        key: getattr(statement.excluded, key)
                        for key in (
                            "views",
                            "clicks",
                            "spend",
                            "atbs",
                            "orders",
                            "canceled",
                            "shks",
                            "revenue",
                            "updated_at",
                        )
                    },
                )
                session.execute(statement)
            session.commit()
        return len(rows)
