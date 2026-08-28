from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import httpx

from app.services.integrations.base import (
    IntegrationError,
    MetricPayload,
    ProductMetric,
    SyncPayload,
    on_date,
    request_json,
)
from app.services.metrics import decimal, safe_divide


class WildberriesIntegration:
    STATISTICS_URL = "https://statistics-api.wildberries.ru"
    ADVERT_URL = "https://advert-api.wildberries.ru"
    ADVERT_STATS_BATCH_SIZE = 50
    ADVERT_STATS_INTERVAL_SECONDS = 20.0

    def __init__(self, credentials: dict[str, str], timeout: float = 45.0):
        self.token = credentials.get("api_token", "").strip()
        self.advert_token = credentials.get("advert_token", "").strip() or self.token
        self.timeout = timeout
        if not self.token:
            raise IntegrationError("Не указан API-токен Wildberries")

    async def validate(self) -> tuple[bool, str]:
        async with httpx.AsyncClient(timeout=self.timeout, headers={"Authorization": self.token}) as client:
            try:
                await request_json(
                    client,
                    "GET",
                    f"{self.STATISTICS_URL}/api/v1/supplier/orders",
                    params={"dateFrom": date.today().isoformat(), "flag": 1},
                    attempts=1,
                )
                return True, "Подключение Wildberries подтверждено"
            except IntegrationError as exc:
                return False, f"Wildberries: {exc}"

    async def fetch(self, target_date: date) -> SyncPayload:
        headers = {"Authorization": self.token}
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            orders = await request_json(
                client,
                "GET",
                f"{self.STATISTICS_URL}/api/v1/supplier/orders",
                # flag=1 asks WB for the exact calendar day.  flag=0 is an
                # incremental lastChangeDate feed and may be truncated, so it
                # cannot be used to reproduce the daily seller summary.
                params={"dateFrom": target_date.isoformat(), "flag": 1},
            )
            sales = await request_json(
                client,
                "GET",
                f"{self.STATISTICS_URL}/api/v1/supplier/sales",
                params={"dateFrom": target_date.isoformat(), "flag": 1},
            )

        products: dict[str, ProductMetric] = {}
        warnings: list[str] = []
        incomplete_prices = 0
        incomplete_payments = 0

        def product_for(item: dict) -> ProductMetric:
            external_id = str(item.get("nmId") or item.get("barcode") or item.get("supplierArticle") or "unknown")
            if external_id not in products:
                products[external_id] = ProductMetric(
                    external_id=external_id,
                    offer_id=str(item.get("supplierArticle") or "") or None,
                    name=str(item.get("supplierArticle") or item.get("subject") or f"Товар {external_id}"),
                    category=str(item.get("subject") or item.get("category") or "Без категории"),
                )
            return products[external_id]

        seen_orders: set[str] = set()
        for item in orders if isinstance(orders, list) else []:
            if not on_date(item.get("date"), target_date):
                continue
            order_id = str(item.get("srid") or item.get("gNumber") or f"{item.get('nmId')}:{item.get('date')}")
            if order_id in seen_orders:
                continue
            seen_orders.add(order_id)
            metric = product_for(item).metrics
            # The WB summary column is explicitly based on the retail price
            # with the agreed discount.  Cancellations remain orders in that
            # gross metric and must not be silently removed here.
            order_amount, is_incomplete = self._summary_price(item)
            incomplete_prices += int(is_incomplete)
            metric.ordered_units += 1
            metric.ordered_amount += order_amount

        seen_sales: set[str] = set()
        for item in sales if isinstance(sales, list) else []:
            if not on_date(item.get("date"), target_date):
                continue
            sale_id = str(item.get("saleID") or item.get("srid") or f"{item.get('nmId')}:{item.get('date')}")
            if sale_id in seen_sales:
                continue
            seen_sales.add(sale_id)
            sale_amount, is_incomplete = self._summary_price(item)
            incomplete_prices += int(is_incomplete)
            raw_for_pay = item.get("forPay")
            for_pay = decimal(raw_for_pay)
            if raw_for_pay in (None, "") or (for_pay == 0 and sale_amount != 0):
                incomplete_payments += 1
            is_return = (
                sale_id.upper().startswith("R")
                or bool(item.get("isStorno"))
                or sale_amount < 0
                or for_pay < 0
            )
            metric = product_for(item).metrics
            metric.buyout_units += -1 if is_return else 1
            metric.buyout_amount += -abs(sale_amount) if is_return else sale_amount
            metric.net_revenue += -abs(for_pay) if is_return else for_pay

        if incomplete_prices:
            warnings.append(
                "WB ещё не заполнил priceWithDisc у "
                f"{incomplete_prices} позиций. Суммы за день предварительные; "
                "повторите синхронизацию позже."
            )
        if incomplete_payments:
            warnings.append(
                "WB ещё не заполнил forPay у "
                f"{incomplete_payments} позиций. Значение «К перечислению за товар» "
                "предварительное; повторите синхронизацию позже."
            )

        advertising_cash, advertising_bonus, advertising_warnings = await self._attach_advertising(
            products,
            target_date,
        )
        warnings.extend(advertising_warnings)

        total = MetricPayload()
        for product in products.values():
            total.add(product.metrics)
        authoritative_total_fields: set[str] = set()
        if advertising_cash is not None:
            total.ad_spend = advertising_cash
            total.ad_bonus_spend = advertising_bonus or Decimal("0")
            authoritative_total_fields.update({"ad_spend", "ad_bonus_spend"})
        return SyncPayload(
            total=total,
            products=list(products.values()),
            warnings=warnings,
            authoritative_total_fields=authoritative_total_fields,
        )

    @staticmethod
    def _summary_price(item: dict) -> tuple[Decimal, bool]:
        """Return the price basis used by WB's seller summary.

        WB documents ``priceWithDisc`` as the retail price with the agreed
        discount.  Falling back to ``finishedPrice`` changes the business
        meaning of the column and was the primary source of reconciliation
        differences.
        """

        raw_value = item.get("priceWithDisc")
        amount = decimal(raw_value)
        raw_total = decimal(item.get("totalPrice"))
        incomplete = raw_value in (None, "") or (amount == 0 and raw_total != 0)
        return amount, incomplete

    async def _attach_advertising(
        self,
        products: dict[str, ProductMetric],
        target_date: date,
    ) -> tuple[Decimal | None, Decimal | None, list[str]]:
        if not self.advert_token:
            return None, None, []
        headers = {"Authorization": self.advert_token}
        warnings: list[str] = []
        source_totals: dict[int, tuple[Decimal, Decimal]] | None = None
        cash_total: Decimal | None = None
        bonus_total: Decimal | None = None
        empty_cost_history = False
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            try:
                cost_history = await request_json(
                    client,
                    "GET",
                    f"{self.ADVERT_URL}/adv/v1/upd",
                    params={"from": target_date.isoformat(), "to": target_date.isoformat()},
                    attempts=5,
                )
                if not isinstance(cost_history, list):
                    raise IntegrationError("WB вернул неожиданный формат истории затрат")
                if cost_history:
                    source_totals = self._source_totals(cost_history)
                    cash_total = sum((cash for cash, _ in source_totals.values()), Decimal("0"))
                    bonus_total = sum((bonus for _, bonus in source_totals.values()), Decimal("0"))
                else:
                    # The expense history can lag behind fullstats.  Preserve
                    # the visible spend as cash instead of overwriting it with
                    # an authoritative-looking zero.
                    empty_cost_history = True
            except IntegrationError as exc:
                warnings.append(
                    "Источники списания WB не разделены: "
                    f"{exc}. Расход сохранён как РК Денег."
                )
            try:
                campaign_payload = await request_json(
                    client, "GET", f"{self.ADVERT_URL}/adv/v1/promotion/count", attempts=2
                )
                ids: list[int] = []
                for group in (campaign_payload or {}).get("adverts", []):
                    status = group.get("status")
                    if status is not None and int(status) not in {7, 9, 11}:
                        continue
                    for advert in group.get("advert_list", []):
                        if advert.get("advertId"):
                            ids.append(int(advert["advertId"]))
                unique_ids = list(dict.fromkeys(ids))
                for offset in range(0, len(unique_ids), self.ADVERT_STATS_BATCH_SIZE):
                    if offset:
                        # Official /adv/v3/fullstats limit: 3 requests/minute,
                        # one request every 20 seconds, burst 1.
                        await asyncio.sleep(self.ADVERT_STATS_INTERVAL_SECONDS)
                    stats = await request_json(
                        client,
                        "GET",
                        f"{self.ADVERT_URL}/adv/v3/fullstats",
                        params={
                            "ids": ",".join(
                                str(value)
                                for value in unique_ids[offset : offset + self.ADVERT_STATS_BATCH_SIZE]
                            ),
                            "beginDate": target_date.isoformat(),
                            "endDate": target_date.isoformat(),
                        },
                        attempts=5,
                    )
                    self._apply_stats(products, stats, target_date, source_totals)
                if empty_cost_history and any(
                    product.metrics.ad_spend or product.metrics.ad_bonus_spend
                    for product in products.values()
                ):
                    warnings.append(
                        "История источников списания WB пока пуста. "
                        "Расход из статистики рекламы временно сохранён как РК Денег."
                    )
            except IntegrationError as exc:
                warnings.append(f"Товарная детализация рекламы WB не обновлена: {exc}")
        return cash_total, bonus_total, warnings

    @staticmethod
    def _source_totals(cost_history: object) -> dict[int, tuple[Decimal, Decimal]]:
        totals: dict[int, tuple[Decimal, Decimal]] = {}
        if not isinstance(cost_history, list):
            return totals
        for item in cost_history:
            if not isinstance(item, dict) or not item.get("advertId"):
                continue
            advert_id = int(item["advertId"])
            cash, bonus = totals.get(advert_id, (Decimal("0"), Decimal("0")))
            amount = decimal(item.get("updSum"))
            payment_type = str(item.get("paymentType") or "").casefold()
            if any(marker in payment_type for marker in ("бонус", "bonus", "кэшбэк", "cashback")):
                bonus += amount
            else:
                # WB cash sources include «Баланс» and «Счёт».
                cash += amount
            totals[advert_id] = (cash, bonus)
        return totals

    @staticmethod
    def _apply_stats(
        products: dict[str, ProductMetric],
        stats: object,
        target_date: date,
        source_totals: dict[int, tuple[Decimal, Decimal]] | None = None,
    ) -> None:
        if not isinstance(stats, list):
            return
        for campaign in stats:
            campaign_entries: list[tuple[MetricPayload, Decimal]] = []
            for day in campaign.get("days", []) if isinstance(campaign, dict) else []:
                if not str(day.get("date", "")).startswith(target_date.isoformat()):
                    continue
                for app in day.get("apps", []):
                    for nm in app.get("nms") or app.get("nm") or []:
                        external_id = str(nm.get("nmId") or nm.get("nm") or "")
                        if not external_id:
                            continue
                        if external_id not in products:
                            products[external_id] = ProductMetric(
                                external_id=external_id,
                                name=str(nm.get("name") or f"Товар {external_id}"),
                                category="Без категории",
                            )
                        metric = products[external_id].metrics
                        spend = decimal(nm.get("sum") or nm.get("spend"))
                        campaign_entries.append((metric, spend))
                        views = decimal(nm.get("views"))
                        clicks = decimal(nm.get("clicks"))
                        if views:
                            metric.ctr = safe_divide(clicks, views)

            advert_id = int(campaign.get("advertId") or 0) if isinstance(campaign, dict) else 0
            fallback_total = sum((spend for _, spend in campaign_entries), Decimal("0"))
            if source_totals is None:
                cash_total, bonus_total = fallback_total, Decimal("0")
            else:
                cash_total, bonus_total = source_totals.get(
                    advert_id,
                    (Decimal("0"), Decimal("0")),
                )
            stats_total = sum((spend for _, spend in campaign_entries), Decimal("0"))
            if not campaign_entries:
                continue
            if stats_total <= 0:
                campaign_entries[0][0].ad_spend += cash_total
                campaign_entries[0][0].ad_bonus_spend += bonus_total
                continue

            allocated_cash = Decimal("0")
            allocated_bonus = Decimal("0")
            for index, (metric, spend) in enumerate(campaign_entries):
                is_last = index == len(campaign_entries) - 1
                cash = cash_total - allocated_cash if is_last else cash_total * spend / stats_total
                bonus = bonus_total - allocated_bonus if is_last else bonus_total * spend / stats_total
                metric.ad_spend += cash
                metric.ad_bonus_spend += bonus
                allocated_cash += cash
                allocated_bonus += bonus
