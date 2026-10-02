from decimal import Decimal
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator
from .domain import now


class ExactDecimal(TypeDecorator):
    """SQLite and PostgreSQL store scaled integers; never round-trip money via float."""
    impl = BigInteger
    cache_ok = True

    def __init__(self, scale=100):
        self.scale = scale
        super().__init__()

    def process_bind_param(self, value, dialect):
        return None if value is None else int((Decimal(str(value)) * self.scale).to_integral_value())

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value) / self.scale


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "marketplace_accounts"
    __table_args__ = (UniqueConstraint("owner_id", "marketplace"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(index=True)
    marketplace: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(160))
    client_id: Mapped[str] = mapped_column(String(80), default="")
    secret: Mapped[str | None] = mapped_column(Text)
    credential_version: Mapped[int] = mapped_column(default=0)
    synced_credential_version: Mapped[int] = mapped_column(default=-1)
    status: Mapped[str] = mapped_column(default="not_configured")
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    last_checked_at: Mapped[object | None] = mapped_column(DateTime)
    last_sync_at: Mapped[object | None] = mapped_column(DateTime)
    last_successful_sync_at: Mapped[object | None] = mapped_column(DateTime)
    last_failed_sync_at: Mapped[object | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)


class Policy(Base):
    __tablename__ = "sync_policies"
    owner_id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_time: Mapped[str | None] = mapped_column(String(5))
    timezone: Mapped[str] = mapped_column(default="Europe/Moscow")
    stale_hours: Mapped[int] = mapped_column(default=24)
    last_scheduled_date: Mapped[str | None] = mapped_column(String(10))
    price_source: Mapped[str] = mapped_column(default="storefront")
    city: Mapped[str] = mapped_column(default="Москва")
    price_basis: Mapped[str] = mapped_column(default="loyalty_price")
    context_version: Mapped[int] = mapped_column(default=1)
    page_interval_seconds: Mapped[int] = mapped_column(default=2)
    browser_client_id: Mapped[str | None] = mapped_column(String(36))


class Product(Base):
    __tablename__ = "marketplace_products"
    __table_args__ = (UniqueConstraint("account_id", "external_id"), Index("ix_product_article", "owner_id", "seller_article"))
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("marketplace_accounts.id"))
    marketplace: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(80), index=True)
    sku: Mapped[str | None] = mapped_column(String(80), index=True)
    seller_article: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(120))
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=now)
    updated_at: Mapped[object] = mapped_column(DateTime, default=now)
    current_snapshot_id: Mapped[int | None] = mapped_column(Integer)
    price_source: Mapped[str] = mapped_column(default="seller_api")
    price_context_version: Mapped[int] = mapped_column(default=0)
    collection_status: Mapped[str] = mapped_column(default="not_collected")
    collection_error: Mapped[str | None] = mapped_column(String(80))


class Mapping(Base):
    __tablename__ = "product_mappings"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(index=True)
    wb_product_id: Mapped[int | None] = mapped_column(ForeignKey("marketplace_products.id"), unique=True)
    ozon_product_id: Mapped[int | None] = mapped_column(ForeignKey("marketplace_products.id"), unique=True)
    mapping_type: Mapped[str] = mapped_column(default="automatic")
    status: Mapped[str] = mapped_column(default="mapping_required", index=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=now)
    updated_at: Mapped[object] = mapped_column(DateTime, default=now, index=True)


class Snapshot(Base):
    __tablename__ = "price_snapshots"
    __table_args__ = (Index("ix_snapshot_product_captured", "product_id", "captured_at"), UniqueConstraint("product_id", "job_id"))
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("marketplace_products.id"))
    base_price: Mapped[Decimal | None] = mapped_column(ExactDecimal())
    discount_price: Mapped[Decimal | None] = mapped_column(ExactDecimal())
    loyalty_price: Mapped[Decimal | None] = mapped_column(ExactDecimal())
    currency: Mapped[str | None] = mapped_column(String(8))
    sources: Mapped[dict] = mapped_column(JSON)
    reasons: Mapped[dict] = mapped_column(JSON)
    captured_at: Mapped[object] = mapped_column(DateTime, default=now)
    job_id: Mapped[int] = mapped_column(ForeignKey("sync_jobs.id"))
    source_kind: Mapped[str] = mapped_column(default="seller_api", index=True)
    context_version: Mapped[int] = mapped_column(default=0)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    page_url: Mapped[str | None] = mapped_column(Text)
    evidence_key: Mapped[str | None] = mapped_column(String(64))


class Comparison(Base):
    __tablename__ = "price_comparisons"
    __table_args__ = (UniqueConstraint("mapping_id", "price_type"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("product_mappings.id", ondelete="CASCADE"), index=True)
    price_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    signed_difference: Mapped[Decimal | None] = mapped_column(ExactDecimal())
    difference_absolute: Mapped[Decimal | None] = mapped_column(ExactDecimal(), index=True)
    difference_percent: Mapped[Decimal | None] = mapped_column(ExactDecimal(10000), index=True)
    direction: Mapped[str | None] = mapped_column(String(24))


class Job(Base):
    __tablename__ = "sync_jobs"
    __table_args__ = (Index("ix_job_queue", "finished_at", "started_at"), Index("ix_job_owner_created", "owner_id", "created_at"))
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(index=True)
    marketplace: Mapped[str | None] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(default="sync")
    trigger: Mapped[str] = mapped_column(default="manual")
    status: Mapped[str] = mapped_column(default="running")
    phase: Mapped[str] = mapped_column(default="queued")
    created_at: Mapped[object] = mapped_column(DateTime, default=now)
    started_at: Mapped[object | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[object | None] = mapped_column(DateTime)
    finished_at: Mapped[object | None] = mapped_column(DateTime)
    products_received: Mapped[int] = mapped_column(default=0)
    products_updated: Mapped[int] = mapped_column(default=0)
    prices_updated: Mapped[int] = mapped_column(default=0)
    errors_count: Mapped[int] = mapped_column(default=0)
    error_summary: Mapped[str | None] = mapped_column(Text)
    results: Mapped[dict] = mapped_column(JSON, default=dict)
    target_product_id: Mapped[int | None] = mapped_column(ForeignKey("marketplace_products.id"))


class CollectionAttempt(Base):
    __tablename__ = "collection_attempts"
    __table_args__ = (Index("ix_attempt_product_date", "product_id", "captured_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("marketplace_products.id"))
    job_id: Mapped[int] = mapped_column(ForeignKey("sync_jobs.id"), index=True)
    captured_at: Mapped[object] = mapped_column(DateTime, default=now)
    status: Mapped[str] = mapped_column(String(40))
    error_code: Mapped[str | None] = mapped_column(String(80))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class ActiveJob(Base):
    __tablename__ = "active_jobs"
    owner_id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("sync_jobs.id"), unique=True)


class StagedProduct(Base):
    __tablename__ = "staged_products"
    __table_args__ = (UniqueConstraint("job_id", "account_id", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("sync_jobs.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("marketplace_accounts.id"))
    external_id: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON)


class Event(Base):
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("sync_jobs.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("marketplace_products.id"))
    event: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime, default=now, index=True)


class BrowserRun(Base):
    __tablename__ = "browser_runs"
    job_id: Mapped[int] = mapped_column(ForeignKey("sync_jobs.id"), primary_key=True)
    client_id: Mapped[str | None] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(default="waiting_browser")
    context_version: Mapped[int] = mapped_column(default=0)
    attention_code: Mapped[str | None] = mapped_column(String(40))


class BrowserTask(Base):
    __tablename__ = "browser_tasks"
    __table_args__ = (UniqueConstraint("job_id", "product_id"), Index("ix_browser_task_queue", "job_id", "state", "id"))
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("sync_jobs.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("marketplace_products.id"))
    token: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(default="pending")
    error_code: Mapped[str | None] = mapped_column(String(40))
