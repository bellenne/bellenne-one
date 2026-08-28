from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    external_user_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True, index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    cabinets: Mapped[list["Cabinet"]] = relationship(back_populates="user")
    templates: Mapped[list["ReplyTemplate"]] = relationship(back_populates="user")


class Cabinet(Base):
    __tablename__ = "cabinets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    marketplace: Mapped[str] = mapped_column(String(20))
    wb_api_key: Mapped[str] = mapped_column(Text, default="")
    ozon_client_id: Mapped[str] = mapped_column(String(120), default="")
    ozon_api_key: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[User] = relationship(back_populates="cabinets")


class ReplyTemplate(Base):
    __tablename__ = "reply_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    match_mode: Mapped[str] = mapped_column(String(20), default="all")
    match_value: Mapped[str] = mapped_column(String(180), default="")
    review_kind: Mapped[str] = mapped_column(String(20), default="any")
    rating_from: Mapped[int] = mapped_column(Integer, default=1)
    rating_to: Mapped[int] = mapped_column(Integer, default=5)
    body: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[User] = relationship(back_populates="templates")


class ScheduleSetting(Base):
    __tablename__ = "schedule_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    max_replies_per_run: Mapped[int] = mapped_column(Integer, default=20)
    quiet_start: Mapped[str] = mapped_column(String(5), default="")
    quiet_end: Mapped[str] = mapped_column(String(5), default="")
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReplyLog(Base):
    __tablename__ = "reply_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    cabinet_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cabinets.id"), nullable=True)
    marketplace: Mapped[str] = mapped_column(String(20))
    review_id: Mapped[str] = mapped_column(String(160), index=True)
    product_name: Mapped[str] = mapped_column(String(240), default="")
    article: Mapped[str] = mapped_column(String(160), default="")
    category: Mapped[str] = mapped_column(String(160), default="")
    review_kind: Mapped[str] = mapped_column(String(20), default="text")
    rating: Mapped[int] = mapped_column(Integer, default=0)
    response_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="sent")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ProcessingQueueItem(Base):
    __tablename__ = "processing_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    cabinet_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cabinets.id"), nullable=True, index=True)
    cabinet_name: Mapped[str] = mapped_column(String(120), default="")
    marketplace: Mapped[str] = mapped_column(String(20))
    review_id: Mapped[str] = mapped_column(String(160), index=True)
    product_name: Mapped[str] = mapped_column(String(240), default="")
    article: Mapped[str] = mapped_column(String(160), default="")
    category: Mapped[str] = mapped_column(String(160), default="")
    detected_categories: Mapped[str] = mapped_column(String(240), default="")
    review_kind: Mapped[str] = mapped_column(String(20), default="text")
    rating: Mapped[int] = mapped_column(Integer, default=0)
    rating_raw: Mapped[str] = mapped_column(String(80), default="")
    rating_source: Mapped[str] = mapped_column(String(80), default="")
    template_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    template_name: Mapped[str] = mapped_column(String(120), default="")
    response_text: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(String(20), default="send")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    message: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0, index=True)
    expected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
