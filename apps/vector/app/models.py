from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(160), default="Основной кабинет")
    encrypted_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    scheduler: Mapped["SchedulerSetting | None"] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        uselist=False,
    )
    user: Mapped["User | None"] = relationship(back_populates="account")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
    )
    external_user_id: Mapped[int | None] = mapped_column(
        Integer,
        unique=True,
        index=True,
        nullable=True,
    )
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    account: Mapped[Account | None] = relationship(
        back_populates="user",
        uselist=False,
    )


class SchedulerSetting(Base):
    __tablename__ = "scheduler_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), unique=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    run_time: Mapped[time] = mapped_column(Time, default=time(5, 15))
    timezone_name: Mapped[str] = mapped_column(
        String(80), default="Europe/Moscow"
    )
    lookback_days: Mapped[int] = mapped_column(Integer, default=3)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    account: Mapped[Account] = relationship(back_populates="scheduler")


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint("account_id", "advert_id", name="uq_campaign_account"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    advert_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    campaign_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("account_id", "nm_id", name="uq_product_account"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    nm_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(300), default="")
    subject_name: Mapped[str] = mapped_column(String(200), default="")
    report_group: Mapped[str] = mapped_column(
        String(200), default="Без категории"
    )
    group_is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProductComment(Base):
    __tablename__ = "product_comments"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "nm_id",
            name="uq_product_comment_account_nm",
        ),
        Index(
            "ix_product_comments_account_updated",
            "account_id",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        index=True,
    )
    nm_id: Mapped[int] = mapped_column(BigInteger)
    comment: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class DailyStat(Base):
    __tablename__ = "daily_stats"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "advert_id",
            "nm_id",
            "stat_date",
            name="uq_daily_stat_scope",
        ),
        Index("ix_daily_stats_account_date", "account_id", "stat_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE")
    )
    advert_id: Mapped[int] = mapped_column(BigInteger)
    nm_id: Mapped[int] = mapped_column(BigInteger)
    stat_date: Mapped[date] = mapped_column(Date)
    views: Mapped[int] = mapped_column(BigInteger, default=0)
    clicks: Mapped[int] = mapped_column(BigInteger, default=0)
    spend: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    atbs: Mapped[int] = mapped_column(BigInteger, default=0)
    orders: Mapped[int] = mapped_column(BigInteger, default=0)
    canceled: Mapped[int] = mapped_column(BigInteger, default=0)
    shks: Mapped[int] = mapped_column(BigInteger, default=0)
    revenue: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        Index("ix_sync_runs_account_started", "account_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE")
    )
    trigger: Mapped[str] = mapped_column(String(24))
    requested_from: Mapped[date] = mapped_column(Date)
    requested_to: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default="running")
    records_received: Mapped[int] = mapped_column(Integer, default=0)
    records_upserted: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
