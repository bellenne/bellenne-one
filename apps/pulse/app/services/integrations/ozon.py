from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import httpx

from app.services.integrations.base import IntegrationError, MetricPayload, ProductMetric, SyncPayload, request_json
from app.services.metrics import decimal


class OzonIntegration:
    BASE_URL = "https://api-seller.ozon.ru"

    def __init__(self, credentials: dict[str, str], timeout: float = 45.0):
        self.client_id = credentials.get("client_id", "").strip()
        self.api_key = credentials.get("api_key", "").strip()
        self.timeout = timeout
        if not self.client_id or not self.api_key:
            raise IntegrationError("Укажите Client-Id и Api-Key Ozon")

    @property
    def headers(self) -> dict[str, str]:
        return {"Client-Id": self.client_id, "Api-Key": self.api_key, "Content-Type": "application/json"}

    async def validate(self) -> tuple[bool, str]:
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
            try:
                payload = await request_json(client, "POST", f"{self.BASE_URL}/v1/seller/info", json={}, attempts=1)
                name = payload.get("name") or payload.get("company", {}).get("name") or "кабинет"
                return True, f"Подключение Ozon подтверждено: {name}"
            except IntegrationError as exc:
                return False, f"Ozon: {exc}"

    async def fetch(self, target_date: date) -> SyncPayload:
        products: dict[str, ProductMetric] = {}
        warnings: list[str] = []
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
            try:
                await self._fetch_analytics(client, products, target_date)
            except IntegrationError as exc:
                raise IntegrationError(f"Не удалось получить аналитику Ozon: {exc}") from exc
            try:
                await self._attach_finance(client, products, target_date)
            except IntegrationError as exc:
                warnings.append(f"Финансы Ozon не обновлены: {exc}")
        total = MetricPayload()
        for product in products.values():
            total.add(product.metrics)
        return SyncPayload(total=total, products=list(products.values()), warnings=warnings)

    async def _fetch_analytics(self, client: httpx.AsyncClient, products: dict[str, ProductMetric], target_date: date) -> None:
        metrics = ["ordered_units", "revenue", "delivered_units"]
        body = {
            "date_from": target_date.isoformat(),
            "date_to": target_date.isoformat(),
            "metrics": metrics,
            "dimension": ["sku"],
            "filters": [],
            "sort": [{"key": "ordered_units", "order": "DESC"}],
            "limit": 1000,
            "offset": 0,
        }
        try:
            payload = await request_json(client, "POST", f"{self.BASE_URL}/v1/analytics/data", json=body, attempts=2)
        except IntegrationError:
            metrics = ["ordered_units", "revenue"]
            body["metrics"] = metrics
            payload = await request_json(client, "POST", f"{self.BASE_URL}/v1/analytics/data", json=body, attempts=2)
        data = payload.get("result", {}).get("data", []) if isinstance(payload, dict) else []
        for row in data:
            dimensions = row.get("dimensions") or []
            dimension = dimensions[0] if dimensions else {}
            external_id = str(dimension.get("id") or dimension.get("value") or dimension.get("name") or "")
            if not external_id:
                continue
            name = str(dimension.get("name") or f"SKU {external_id}")
            metric_values = row.get("metrics") or []
            metric_map = {key: decimal(metric_values[index]) if index < len(metric_values) else Decimal("0") for index, key in enumerate(metrics)}
            ordered_units = int(metric_map.get("ordered_units", Decimal("0")))
            revenue = metric_map.get("revenue", Decimal("0"))
            delivered = int(metric_map.get("delivered_units", Decimal("0")))
            products[external_id] = ProductMetric(
                external_id=external_id,
                offer_id=str(dimension.get("id") or "") or None,
                name=name,
                category=name,
                metrics=MetricPayload(
                    ordered_units=ordered_units,
                    ordered_amount=revenue,
                    buyout_units=delivered,
                ),
            )

    async def _attach_finance(self, client: httpx.AsyncClient, products: dict[str, ProductMetric], target_date: date) -> None:
        page = 1
        page_count = 1
        while page <= page_count:
            body = {
                "filter": {
                    "date": {
                        "from": f"{target_date.isoformat()}T00:00:00.000Z",
                        "to": f"{target_date.isoformat()}T23:59:59.999Z",
                    },
                    "operation_type": [],
                    "posting_number": "",
                    "transaction_type": "all",
                },
                "page": page,
                "page_size": 1000,
            }
            payload = await request_json(client, "POST", f"{self.BASE_URL}/v3/finance/transaction/list", json=body, attempts=2)
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            page_count = int(result.get("page_count") or 1)
            for operation in result.get("operations", []):
                self._apply_operation(products, operation)
            page += 1

    @staticmethod
    def _apply_operation(products: dict[str, ProductMetric], operation: dict) -> None:
        items = operation.get("items") or operation.get("posting", {}).get("items") or []
        operation_name = str(operation.get("operation_type_name") or operation.get("operation_type") or "").lower()
        accrual = decimal(operation.get("accruals_for_sale"))
        commission = decimal(operation.get("sale_commission"))
        services = sum((decimal(item.get("price")) for item in operation.get("services", [])), Decimal("0"))
        net = accrual + commission + services
        ad_cost = abs(net) if any(term in operation_name for term in ("реклам", "promotion", "advert")) and net < 0 else Decimal("0")
        if not items:
            return
        weight = Decimal("1") / Decimal(len(items))
        for item in items:
            external_id = str(item.get("sku") or item.get("product_id") or item.get("offer_id") or "")
            if not external_id:
                continue
            if external_id not in products:
                products[external_id] = ProductMetric(
                    external_id=external_id,
                    offer_id=str(item.get("offer_id") or "") or None,
                    name=str(item.get("name") or f"SKU {external_id}"),
                    category=str(item.get("name") or "Без категории"),
                )
            metric = products[external_id].metrics
            metric.net_revenue += net * weight
            metric.ad_spend += ad_cost * weight
            if "достав" in operation_name or "delivered" in operation_name:
                quantity = int(item.get("quantity") or 1)
                metric.buyout_units = max(metric.buyout_units, quantity)
                metric.buyout_amount += max(accrual * weight, Decimal("0"))

