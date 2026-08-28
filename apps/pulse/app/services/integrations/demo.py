from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

from app.models import Marketplace
from app.services.integrations.base import MetricPayload, ProductMetric, SyncPayload


DEMO_CATEGORIES = (
    ("wallpaper", "Фотообои", Decimal("4300"), 68),
    ("shirts", "Футболки", Decimal("1250"), 21),
    ("fence", "Фотосетки для забора", Decimal("2750"), 8),
)


class DemoIntegration:
    def __init__(self, marketplace: str):
        self.marketplace = marketplace

    async def validate(self) -> tuple[bool, str]:
        return True, "Демо-кабинет готов"

    async def fetch(self, target_date: date) -> SyncPayload:
        products: list[ProductMetric] = []
        total = MetricPayload()
        market_factor = Decimal("1") if self.marketplace == Marketplace.WB.value else Decimal("0.72")
        day_factor = Decimal(str(1 + 0.16 * math.sin(target_date.toordinal() / 3.7)))
        for index, (external_id, name, check, base_units) in enumerate(DEMO_CATEGORIES):
            product_factor = market_factor * day_factor * Decimal(str(1 + index * 0.06))
            ordered_units = max(int(Decimal(base_units) * product_factor), 0)
            ordered_amount = (Decimal(ordered_units) * check).quantize(Decimal("0.01"))
            buyout_rate = Decimal("0.71") + Decimal(str(0.04 * math.sin((target_date.toordinal() + index) / 5)))
            buyout_units = max(int(Decimal(ordered_units) * buyout_rate), 0)
            buyout_amount = (Decimal(buyout_units) * check * Decimal("0.96")).quantize(Decimal("0.01"))
            cash = (ordered_amount * (Decimal("0.055") + Decimal(index) * Decimal("0.004"))).quantize(Decimal("0.01"))
            bonus = (cash * Decimal("0.35") if self.marketplace == Marketplace.WB.value else Decimal("0")).quantize(Decimal("0.01"))
            metrics = MetricPayload(
                ordered_units=ordered_units,
                ordered_amount=ordered_amount,
                buyout_units=buyout_units,
                buyout_amount=buyout_amount,
                net_revenue=(buyout_amount * Decimal("0.72")).quantize(Decimal("0.01")),
                ad_spend=cash,
                ad_bonus_spend=bonus,
                ctr=Decimal("0.064") + Decimal(index) * Decimal("0.004"),
            )
            total.add(metrics)
            products.append(
                ProductMetric(
                    external_id=f"{self.marketplace.lower()}-{external_id}",
                    offer_id=external_id.upper(),
                    name=name,
                    category=name,
                    metrics=metrics,
                )
            )
        return SyncPayload(total=total, products=products)

