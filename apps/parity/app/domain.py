from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

PRICE_TYPES = ("base_price", "discount_price", "loyalty_price")
PRICE_LABELS = {"base_price": "Базовая", "discount_price": "Со скидкой", "loyalty_price": "Лояльность"}


def now():
    # Like the other SQLite modules: naive UTC in DB, localized only in views.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def money(value):
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal("99999999999999.99"):
            raise ValueError("invalid_price")
        return amount.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError("invalid_price") from None


@dataclass
class NormalizedPrice:
    base_price: Decimal | None = None
    discount_price: Decimal | None = None
    loyalty_price: Decimal | None = None
    currency: str | None = None
    sources: dict = field(default_factory=dict)
    reasons: dict = field(default_factory=dict)

    def payload(self):
        return {**asdict(self), **{key: str(getattr(self, key)) if getattr(self, key) is not None else None for key in PRICE_TYPES}}

    @classmethod
    def from_payload(cls, payload):
        return cls(**{**payload, **{key: money(payload.get(key)) for key in PRICE_TYPES}})


@dataclass
class NormalizedProduct:
    external_id: str
    seller_article: str
    name: str
    sku: str | None
    status: str
    price: NormalizedPrice

    def payload(self):
        return {**asdict(self), "price": self.price.payload()}


def difference(wb, ozon):
    signed = ozon - wb
    percent = signed / wb * 100 if wb != 0 else None
    return signed, percent
