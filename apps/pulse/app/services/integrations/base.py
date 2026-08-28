from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx

from app.services.metrics import decimal


class IntegrationError(RuntimeError):
    pass


@dataclass
class MetricPayload:
    ordered_units: int = 0
    ordered_amount: Decimal = Decimal("0")
    buyout_units: int = 0
    buyout_amount: Decimal = Decimal("0")
    net_revenue: Decimal = Decimal("0")
    ad_spend: Decimal = Decimal("0")
    ad_bonus_spend: Decimal = Decimal("0")
    ctr: Decimal | None = None

    def add(self, other: MetricPayload) -> MetricPayload:
        self.ordered_units += int(other.ordered_units)
        self.ordered_amount += decimal(other.ordered_amount)
        self.buyout_units += int(other.buyout_units)
        self.buyout_amount += decimal(other.buyout_amount)
        self.net_revenue += decimal(other.net_revenue)
        self.ad_spend += decimal(other.ad_spend)
        self.ad_bonus_spend += decimal(other.ad_bonus_spend)
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "ordered_units": self.ordered_units,
            "ordered_amount": self.ordered_amount,
            "buyout_units": self.buyout_units,
            "buyout_amount": self.buyout_amount,
            "net_revenue": self.net_revenue,
            "ad_spend": self.ad_spend,
            "ad_bonus_spend": self.ad_bonus_spend,
            "ctr": self.ctr,
        }


@dataclass
class ProductMetric:
    external_id: str
    name: str
    category: str | None = None
    offer_id: str | None = None
    metrics: MetricPayload = field(default_factory=MetricPayload)


@dataclass
class SyncPayload:
    total: MetricPayload
    products: list[ProductMetric]
    warnings: list[str] = field(default_factory=list)
    authoritative_total_fields: set[str] = field(default_factory=set)


class MarketplaceIntegration(Protocol):
    async def fetch(self, target_date: date) -> SyncPayload: ...
    async def validate(self) -> tuple[bool, str]: ...


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        try:
            return datetime.fromisoformat(normalized.split(".", 1)[0])
        except ValueError:
            return None


def on_date(value: str | None, target: date) -> bool:
    parsed = parse_datetime(value)
    return bool(parsed and parsed.date() == target)


def retry_delay_seconds(response: httpx.Response, attempt: int) -> float:
    """Return a WB-compatible retry delay for a rate-limited response."""
    header_value = response.headers.get("X-Ratelimit-Retry") or response.headers.get("Retry-After")
    if header_value:
        try:
            return max(float(header_value), 0.0)
        except ValueError:
            pass
    return min(float(2**attempt), 60.0)


async def request_json(client: httpx.AsyncClient, method: str, url: str, *, attempts: int = 3, **kwargs) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.request(method, url, **kwargs)
            if response.status_code == 429 and attempt + 1 < attempts:
                await asyncio.sleep(retry_delay_seconds(response, attempt))
                continue
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt + 1 == attempts:
                break
    raise IntegrationError(str(last_error) if last_error else "Marketplace API request failed")
