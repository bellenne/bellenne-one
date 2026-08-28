from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.clock import utc_now
from app.database import Base


class Marketplace(StrEnum):
    WB = "WB"
    OZON = "OZON"


class PlanMode(StrEnum):
    TOTAL = "total"
    BY_PRODUCT = "by_product"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    accounts: Mapped[list[MarketplaceAccount]] = relationship(back_populates="user", cascade="all, delete-orphan")
    product_groups: Mapped[list[ProductGroup]] = relationship(back_populates="user", cascade="all, delete-orphan")


class MarketplaceAccount(Base):
    __tablename__ = "marketplace_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    marketplace: Mapped[str] = mapped_column(String(16), index=True)
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_time: Mapped[time] = mapped_column(Time, default=time(6, 15))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_sync_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    user: Mapped[User] = relationship(back_populates="accounts")
    products: Mapped[list[MarketplaceProduct]] = relationship(back_populates="account", cascade="all, delete-orphan")
    metrics: Mapped[list[DailyMetric]] = relationship(back_populates="account", cascade="all, delete-orphan")
    sync_runs: Mapped[list[SyncRun]] = relationship(back_populates="account", cascade="all, delete-orphan")
    wb_summary_metrics: Mapped[list[WBSummaryMetric]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
    )


class ProductGroup(Base):
    __tablename__ = "product_groups"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_product_group_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(120), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    user: Mapped[User] = relationship(back_populates="product_groups")
    products: Mapped[list[MarketplaceProduct]] = relationship(back_populates="product_group")


class MarketplaceProduct(Base):
    __tablename__ = "marketplace_products"
    __table_args__ = (UniqueConstraint("account_id", "external_id", name="uq_product_account_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("marketplace_accounts.id", ondelete="CASCADE"), index=True)
    product_group_id: Mapped[int | None] = mapped_column(ForeignKey("product_groups.id", ondelete="SET NULL"), nullable=True, index=True)
    external_id: Mapped[str] = mapped_column(String(160))
    offer_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(160), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    account: Mapped[MarketplaceAccount] = relationship(back_populates="products")
    product_group: Mapped[ProductGroup | None] = relationship(back_populates="products")


class DailyMetric(Base):
    __tablename__ = "daily_metrics"
    __table_args__ = (UniqueConstraint("account_id", "metric_date", "scope_key", name="uq_metric_account_date_scope"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("marketplace_accounts.id", ondelete="CASCADE"), index=True)
    product_group_id: Mapped[int | None] = mapped_column(ForeignKey("product_groups.id", ondelete="SET NULL"), nullable=True, index=True)
    marketplace: Mapped[str] = mapped_column(String(16), index=True)
    metric_date: Mapped[date] = mapped_column(Date, index=True)
    scope_key: Mapped[str] = mapped_column(String(64), default="total")

    ordered_units: Mapped[int] = mapped_column(Integer, default=0)
    ordered_amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    buyout_units: Mapped[int] = mapped_column(Integer, default=0)
    buyout_amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    net_revenue: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    ad_spend: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    ad_bonus_spend: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    gross_profit: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True, default=None)
    ctr: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    tests_count: Mapped[int] = mapped_column(Integer, default=0)
    tests_passed: Mapped[int] = mapped_column(Integer, default=0)
    test_spend: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    liquidated_units: Mapped[int] = mapped_column(Integer, default=0)
    liquidated_amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    source: Mapped[str] = mapped_column(String(32), default="api")
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    account: Mapped[MarketplaceAccount] = relationship(back_populates="metrics")
    product_group: Mapped[ProductGroup | None] = relationship()


class WBSummaryMetric(Base):
    """Authoritative account totals imported from WB's seller summary XLSX."""

    __tablename__ = "wb_summary_metrics"
    __table_args__ = (
        UniqueConstraint("account_id", "metric_date", name="uq_wb_summary_account_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_accounts.id", ondelete="CASCADE"),
        index=True,
    )
    metric_date: Mapped[date] = mapped_column(Date, index=True)
    ordered_units: Mapped[int] = mapped_column(Integer, default=0)
    ordered_amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    buyout_units: Mapped[int] = mapped_column(Integer, default=0)
    buyout_amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    net_revenue: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    source_name: Mapped[str] = mapped_column(String(255))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    account: Mapped[MarketplaceAccount] = relationship(back_populates="wb_summary_metrics")


class MonthlyPlan(Base):
    __tablename__ = "monthly_plans"
    __table_args__ = (UniqueConstraint("user_id", "period", "marketplace", name="uq_plan_user_period_marketplace"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    period: Mapped[date] = mapped_column(Date, index=True)
    marketplace: Mapped[str] = mapped_column(String(16), default="ALL")
    mode: Mapped[str] = mapped_column(String(24), default=PlanMode.TOTAL.value)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    values: Mapped[list[PlanValue]] = relationship(back_populates="plan", cascade="all, delete-orphan")
    allocations: Mapped[list[PlanAllocation]] = relationship(back_populates="plan", cascade="all, delete-orphan")


class PlanValue(Base):
    __tablename__ = "plan_values"
    __table_args__ = (UniqueConstraint("plan_id", "product_group_id", "scope_key", "metric_key", name="uq_plan_value_scope_metric"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("monthly_plans.id", ondelete="CASCADE"), index=True)
    product_group_id: Mapped[int | None] = mapped_column(ForeignKey("product_groups.id", ondelete="CASCADE"), nullable=True)
    scope_key: Mapped[str] = mapped_column(String(64), default="total")
    metric_key: Mapped[str] = mapped_column(String(64))
    monthly_value: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))

    plan: Mapped[MonthlyPlan] = relationship(back_populates="values")
    product_group: Mapped[ProductGroup | None] = relationship()


class PlanAllocation(Base):
    __tablename__ = "plan_allocations"
    __table_args__ = (UniqueConstraint("plan_id", "product_group_id", name="uq_plan_allocation_group"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("monthly_plans.id", ondelete="CASCADE"), index=True)
    product_group_id: Mapped[int] = mapped_column(ForeignKey("product_groups.id", ondelete="CASCADE"), index=True)
    percentage: Mapped[Decimal] = mapped_column(Numeric(7, 4))

    plan: Mapped[MonthlyPlan] = relationship(back_populates="allocations")
    product_group: Mapped[ProductGroup] = relationship()


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("marketplace_accounts.id", ondelete="CASCADE"), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(24), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    account: Mapped[MarketplaceAccount] = relationship(back_populates="sync_runs")
