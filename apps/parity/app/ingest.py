"""Token-scoped, atomic ingestion into existing snapshots and comparisons."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from typing import Literal
from .domain import now
from .models import ActiveJob, IngestEvent, IngestToken, Job, Policy, Product, Snapshot
from .service import log, recalculate


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid")
    marketplace: Literal["wb", "ozon"]
    sku: str = Field(min_length=1, max_length=80, pattern=r"^[0-9]+$")
    observed_at: datetime
    currency: Literal["RUB"] = "RUB"
    loyalty_price: Decimal | None
    discount_price: Decimal | None = None
    base_price: Decimal | None = None

    @field_validator("loyalty_price", "discount_price", "base_price", mode="before")
    @classmethod
    def money(cls, value):
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 20:
            raise ValueError("Prices must be decimal strings")
        amount = Decimal(value)
        if not amount.is_finite() or not 0 < amount <= Decimal("999999999999.99") or amount != amount.quantize(Decimal("0.01")):
            raise ValueError("Invalid price")
        return amount

    @field_validator("observed_at")
    @classmethod
    def timestamp(cls, value):
        if value.tzinfo is None:
            raise ValueError("Timezone required")
        return value.astimezone(timezone.utc)


class Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    event_id: UUID
    prices: list[Price] = Field(min_length=1, max_length=100)


def authenticate(session, authorization):
    if not authorization.startswith("Bearer ") or not 32 <= len(authorization[7:]) <= 256:
        raise HTTPException(401, "Invalid Bearer token")
    token = session.scalar(select(IngestToken).where(IngestToken.digest == digest(authorization[7:])))
    if not token:
        raise HTTPException(401, "Invalid Bearer token")
    return token.owner_id


def accept(session, owner, batch):
    fingerprint = digest(json.dumps(batch.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
    previous = session.scalar(select(IngestEvent).where(IngestEvent.owner_id == owner, IngestEvent.event_id == str(batch.event_id)))
    if previous:
        if previous.digest != fingerprint:
            raise HTTPException(409, "Event ID already used with different data")
        return {"accepted": True, "duplicate": True, "job_id": previous.job_id}
    if session.get(ActiveJob, owner):
        raise HTTPException(409, "Stop or finish the active job before importing prices")
    policy = session.get(Policy, owner)
    resolved, seen = [], set()
    for row in batch.prices:
        stamp = row.observed_at.replace(tzinfo=None)
        if stamp > now() + timedelta(minutes=5) or stamp < now() - timedelta(hours=policy.stale_hours):
            raise HTTPException(422, "Observation is stale or in the future")
        products = list(session.scalars(select(Product).where(Product.owner_id == owner, Product.marketplace == row.marketplace, Product.sku == row.sku, Product.present.is_(True))))
        if not products:
            raise HTTPException(404, "SKU not found in imported catalogue")
        if len(products) != 1 or products[0].id in seen:
            raise HTTPException(409, "Ambiguous or repeated SKU")
        product = products[0]
        latest = session.get(Snapshot, product.current_snapshot_id) if product.current_snapshot_id else None
        if latest and stamp <= latest.captured_at:
            raise HTTPException(409, "Observation must be newer than the current price")
        seen.add(product.id)
        resolved.append((product, row, stamp))
    if policy.price_source != "storefront" or policy.browser_client_id != "external_api":
        policy.context_version += 1
    policy.price_source, policy.browser_client_id, policy.enabled = "storefront", "external_api", False
    job = Job(owner_id=owner, kind="external_api", trigger="api", status="success", phase="complete", started_at=now(), finished_at=now(), products_received=len(resolved), products_updated=len(resolved), prices_updated=len(resolved))
    session.add(job)
    session.flush()
    for product, row, stamp in resolved:
        prices = {key: getattr(row, key) for key in ("base_price", "discount_price", "loyalty_price")}
        snapshot = Snapshot(product_id=product.id, job_id=job.id, captured_at=stamp, currency="RUB", source_kind="storefront", context_version=policy.context_version,
            context={"collector": "external_api", "session": "external_application", "payment": "wb_wallet" if row.marketplace == "wb" else "ozon_card"},
            sources={key: "external_api" for key, value in prices.items() if value is not None}, reasons={key: "not_provided" for key, value in prices.items() if value is None}, **prices)
        session.add(snapshot)
        session.flush()
        product.current_snapshot_id, product.updated_at = snapshot.id, stamp
        product.price_source, product.price_context_version = "storefront", policy.context_version
        product.collection_status, product.collection_error = "success", None
    session.add(IngestEvent(owner_id=owner, event_id=str(batch.event_id), digest=fingerprint, job_id=job.id))
    recalculate(session, owner, job.id)
    log(session, owner, "sync_finish", job.id, status="success", collector="external_api")
    return {"accepted": True, "duplicate": False, "job_id": job.id}
