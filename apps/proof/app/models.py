from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ProofPreset(Base):
    __tablename__ = "proof_presets"
    __table_args__ = (UniqueConstraint("owner_external_user_id", "logical_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_external_user_id: Mapped[int] = mapped_column(Integer, index=True)
    logical_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(160))
    version: Mapped[int] = mapped_column(Integer)
    parameters_json: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ProofWorker(Base):
    __tablename__ = "proof_workers"
    __table_args__ = (UniqueConstraint("owner_external_user_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_external_user_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(160))
    hostname: Mapped[str] = mapped_column(String(255), default="")
    availability: Mapped[str] = mapped_column(String(24), default="available")
    version: Mapped[str] = mapped_column(String(80), default="")
    capabilities_json: Mapped[str] = mapped_column(Text, default="[]")
    current_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    heartbeat_timeout_seconds: Mapped[int] = mapped_column(Integer)
    callback_url: Mapped[str] = mapped_column(String(1000), default="")
    configuration_json: Mapped[str] = mapped_column(Text, default="{}")
    configuration_version: Mapped[int] = mapped_column(Integer, default=1)
    last_error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(16))
    token_last_four: Mapped[str] = mapped_column(String(4))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class ProofIntegration(Base):
    __tablename__ = "proof_integrations"
    __table_args__ = (UniqueConstraint("owner_external_user_id", "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_external_user_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(40), default="amocrm")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    trigger_events_json: Mapped[str] = mapped_column(Text, default="[]")
    configuration_json: Mapped[str] = mapped_column(Text, default="{}")
    default_preset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_url: Mapped[str] = mapped_column(String(1000), default="")
    credentials_encrypted: Mapped[str] = mapped_column(Text, default="")
    webhook_secret_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    webhook_secret_prefix: Mapped[str] = mapped_column(String(16))
    webhook_secret_last_four: Mapped[str] = mapped_column(String(4))
    oauth_state_digest: Mapped[str] = mapped_column(String(64), default="")
    oauth_state_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_incoming_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class WebhookReceipt(Base):
    __tablename__ = "proof_webhook_receipts"
    __table_args__ = (UniqueConstraint("integration_id", "idempotency_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    integration_id: Mapped[int] = mapped_column(ForeignKey("proof_integrations.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(120))
    payload_json: Mapped[str] = mapped_column(Text)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ProofJob(Base):
    __tablename__ = "proof_jobs"
    __table_args__ = (
        Index("ix_proof_jobs_queue", "owner_external_user_id", "processing_status", "queued_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_external_user_id: Mapped[int] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(40))
    external_event_id: Mapped[str] = mapped_column(String(128), index=True)
    crm_entity_type: Mapped[str] = mapped_column(String(80), default="")
    crm_entity_id: Mapped[str] = mapped_column(String(120), default="")
    crm_order_id: Mapped[str] = mapped_column(String(120), default="")
    input_json: Mapped[str] = mapped_column(Text)
    processing_status: Mapped[str] = mapped_column(String(24), index=True)
    delivery_status: Mapped[str] = mapped_column(String(24), index=True)
    progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_stage: Mapped[str] = mapped_column(String(160), default="")
    preset_id: Mapped[int] = mapped_column(ForeignKey("proof_presets.id"), index=True)
    preset_logical_id: Mapped[str] = mapped_column(String(36))
    preset_name: Mapped[str] = mapped_column(String(160))
    preset_version: Mapped[int] = mapped_column(Integer)
    preset_snapshot_json: Mapped[str] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(ForeignKey("proof_workers.id"), nullable=True, index=True)
    current_result_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    preset: Mapped[ProofPreset] = relationship()
    worker: Mapped[ProofWorker | None] = relationship(foreign_keys=[worker_id])


class ProofResult(Base):
    __tablename__ = "proof_results"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt"),
        UniqueConstraint("worker_id", "idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_external_user_id: Mapped[int] = mapped_column(Integer, index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("proof_jobs.id"), index=True)
    worker_id: Mapped[str] = mapped_column(ForeignKey("proof_workers.id"), index=True)
    preset_logical_id: Mapped[str] = mapped_column(String(36))
    preset_version: Mapped[int] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(160))
    file_path: Mapped[str] = mapped_column(Text)
    file_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)

    job: Mapped[ProofJob] = relationship(foreign_keys=[job_id])
    worker: Mapped[ProofWorker] = relationship(foreign_keys=[worker_id])


class ProofResultDelivery(Base):
    __tablename__ = "proof_result_deliveries"
    __table_args__ = (UniqueConstraint("result_id", "integration_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_external_user_id: Mapped[int] = mapped_column(Integer, index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("proof_jobs.id"), index=True)
    result_id: Mapped[str] = mapped_column(ForeignKey("proof_results.id"), index=True)
    integration_id: Mapped[int] = mapped_column(ForeignKey("proof_integrations.id"), index=True)
    archive_filename: Mapped[str] = mapped_column(String(255))
    archive_sha256: Mapped[str] = mapped_column(String(64), default="")
    amo_file_uuid: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    amo_version_uuid: Mapped[str | None] = mapped_column(String(80), nullable=True)
    amo_note_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yandex_disk_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    yandex_public_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    job: Mapped[ProofJob] = relationship(foreign_keys=[job_id])
    result: Mapped[ProofResult] = relationship(foreign_keys=[result_id])
    integration: Mapped[ProofIntegration] = relationship(foreign_keys=[integration_id])


class ProofEvent(Base):
    __tablename__ = "proof_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_external_user_id: Mapped[int] = mapped_column(Integer, index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    worker_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    integration_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(120), index=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    level: Mapped[str] = mapped_column(String(20), index=True)
    message: Mapped[str] = mapped_column(Text)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class ProofNotificationDelivery(Base):
    __tablename__ = "proof_notification_deliveries"
    __table_args__ = (UniqueConstraint("event_id", "integration_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_external_user_id: Mapped[int] = mapped_column(Integer, index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("proof_events.id"), index=True)
    integration_id: Mapped[int] = mapped_column(ForeignKey("proof_integrations.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    event: Mapped[ProofEvent] = relationship(foreign_keys=[event_id])
    integration: Mapped[ProofIntegration] = relationship(foreign_keys=[integration_id])
