from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import time
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from .amocrm import AmoClient, AmoIntegrationConfiguration
from .config import AppSettings
from .models import (
    ProofEvent,
    ProofIntegration,
    ProofJob,
    ProofNotificationDelivery,
    ProofPreset,
    ProofResult,
    ProofResultDelivery,
    ProofWorker,
    WebhookReceipt,
    utc_now,
)
from .security import constant_time_matches, decrypt_secret, new_secret, secret_parts, token_digest


PROCESSING_STATUSES = (
    "received", "queued", "assigned", "running", "completed", "failed", "cancelled", "retrying"
)
DELIVERY_STATUSES = ("pending", "delivering", "delivered", "failed", "retrying")
WORKER_AVAILABILITY = ("available", "busy", "error")
LOG_LEVELS = ("debug", "info", "warning", "error", "critical")

PROCESSING_LABELS = {
    "received": "Получено", "queued": "В очереди", "assigned": "Назначено",
    "running": "Выполняется", "completed": "Завершено", "failed": "Ошибка",
    "cancelled": "Отменено", "retrying": "Повторный запуск",
}
DELIVERY_LABELS = {
    "pending": "Ожидает доставки", "delivering": "Доставляется",
    "delivered": "Доставлено", "failed": "Ошибка доставки", "retrying": "Повторная доставка",
}

WORKER_RUNTIME_DEFAULTS = {
    "heartbeat_interval": 30,
    "poll_interval": 5,
    "retry_initial_seconds": 1,
    "retry_max_seconds": 30,
    "storage_retry_limit": 5,
    "file_not_found_retry_limit": 0,
    "health_interval": 5,
    "health_max_age": 90,
    "path_mappings": [],
}

ALLOWED_PROCESSING_TRANSITIONS = {
    "received": {"queued", "cancelled"},
    "queued": {"assigned", "cancelled"},
    "assigned": {"running", "queued", "failed", "cancelled"},
    "running": {"completed", "failed", "queued", "cancelled"},
    "completed": {"retrying"},
    "failed": {"retrying", "cancelled"},
    "cancelled": {"retrying"},
    "retrying": {"queued", "cancelled"},
}
ALLOWED_DELIVERY_TRANSITIONS = {
    "pending": {"delivering"},
    "delivering": {"delivered", "failed"},
    "delivered": {"pending"},
    "failed": {"retrying", "pending"},
    "retrying": {"delivering"},
}


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def json_load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def sanitized_details(value: Any) -> Any:
    blocked = {"authorization", "access_token", "refresh_token", "token", "secret", "password"}
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).casefold() in blocked else sanitized_details(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitized_details(item) for item in value]
    return value


def sanitized_message(value: str) -> str:
    message = re.sub(
        r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s,;]+",
        r"\1[REDACTED]",
        value,
    )
    message = re.sub(r"\bproof_(?:worker|hook)_[A-Za-z0-9_-]+", "[REDACTED]", message)
    message = re.sub(
        r"(?i)((?:access_token|refresh_token|token|secret|password)\s*[=:]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        message,
    )
    return message


def add_event(
    session: Session,
    *,
    owner_external_user_id: int,
    event_type: str,
    source: str,
    message: str,
    level: str = "info",
    job_id: str | None = None,
    worker_id: str | None = None,
    integration_id: int | None = None,
    details: dict[str, Any] | None = None,
    error_code: str | None = None,
    queue_notification: bool = True,
) -> ProofEvent:
    event = ProofEvent(
        id=str(uuid4()),
        owner_external_user_id=owner_external_user_id,
        job_id=job_id,
        worker_id=worker_id,
        integration_id=integration_id,
        event_type=event_type[:120],
        source=source[:40],
        level=level if level in LOG_LEVELS else "info",
        message=sanitized_message(message)[:4000],
        details_json=json_dump(sanitized_details(details or {})),
        error_code=(error_code or "")[:120] or None,
    )
    session.add(event)
    if event.level in {"error", "critical"} and queue_notification:
        mattermost = session.scalar(select(ProofIntegration).where(
            ProofIntegration.owner_external_user_id == owner_external_user_id,
            ProofIntegration.kind == "mattermost",
            ProofIntegration.enabled.is_(True),
        ))
        if mattermost is not None:
            session.add(ProofNotificationDelivery(
                id=str(uuid4()),
                owner_external_user_id=owner_external_user_id,
                event_id=event.id,
                integration_id=mattermost.id,
                status="pending",
            ))
            session.info["proof_notification_pending"] = True
    return event


def mattermost_settings(
    integration: ProofIntegration, settings: AppSettings
) -> tuple[str, str]:
    credentials = decrypt_secret(
        credential_cipher_for_settings(settings), integration.credentials_encrypted
    )
    configuration = json_load(integration.trigger_events_json, {})
    channel = str(configuration.get("channel", "")) if isinstance(configuration, dict) else ""
    return str(credentials.get("webhook_url", "")), channel[:120]


def mattermost_error_message(event: ProofEvent) -> str:
    def safe(value: str) -> str:
        return value.replace("@", "@\u200b").replace("`", "'").strip()

    lines = [
        "#### BellenneProof: зафиксирована ошибка",
        f"**Сообщение:** {safe(event.message)}",
        f"**Источник:** `{safe(event.source)}`",
        f"**Событие:** `{safe(event.event_type)}`",
    ]
    if event.job_id:
        lines.append(f"**Job:** `{safe(event.job_id)}`")
    if event.worker_id:
        lines.append(f"**Worker:** `{safe(event.worker_id)}`")
    if event.error_code:
        lines.append(f"**Код:** `{safe(event.error_code)}`")
    lines.append(f"**Время:** `{event.created_at.isoformat(timespec='seconds')} UTC`")
    return "\n".join(lines)


def post_mattermost_message(
    settings: AppSettings,
    webhook_url: str,
    channel: str,
    text_value: str,
    *,
    client: httpx.Client | None = None,
) -> int:
    payload: dict[str, Any] = {"text": text_value}
    if channel:
        payload["channel"] = channel
    owns_client = client is None
    http_client = client or httpx.Client(timeout=settings.mattermost_http_timeout_seconds)
    try:
        response = http_client.post(webhook_url, json=payload)
    finally:
        if owns_client:
            http_client.close()
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Mattermost webhook returned HTTP {response.status_code}.")
    return response.status_code


def dispatch_pending_mattermost(
    session_factory: sessionmaker[Session],
    settings: AppSettings,
    *,
    client: httpx.Client | None = None,
) -> int:
    delivered = 0
    with session_factory() as session:
        pending = list(session.scalars(
            select(ProofNotificationDelivery)
            .where(ProofNotificationDelivery.status == "pending")
            .order_by(ProofNotificationDelivery.created_at)
        ))
        for notification in pending:
            event = notification.event
            integration = notification.integration
            webhook_url, channel = mattermost_settings(integration, settings)
            try:
                if not integration.enabled:
                    raise RuntimeError("Mattermost integration is disabled.")
                if not webhook_url:
                    raise RuntimeError("Mattermost webhook URL is not configured.")
                notification.response_status = post_mattermost_message(
                    settings, webhook_url, channel, mattermost_error_message(event), client=client
                )
            except Exception as exc:
                safe_error = sanitized_message(str(exc).replace(webhook_url, "[REDACTED_URL]"))[:2000]
                notification.status = "failed"
                notification.last_error = safe_error
                integration.last_error_at = utc_now()
                integration.last_error_message = safe_error
                add_event(
                    session,
                    owner_external_user_id=notification.owner_external_user_id,
                    job_id=event.job_id,
                    worker_id=event.worker_id,
                    integration_id=integration.id,
                    event_type="mattermost.notification.failed",
                    source="integration",
                    level="error",
                    message="Mattermost notification delivery failed.",
                    error_code="MATTERMOST_DELIVERY_FAILED",
                    details={"error": safe_error, "source_event_id": event.id},
                    queue_notification=False,
                )
            else:
                notification.status = "sent"
                notification.sent_at = utc_now()
                notification.last_error = None
                integration.last_delivery_at = notification.sent_at
                integration.last_error_message = None
                delivered += 1
                add_event(
                    session,
                    owner_external_user_id=notification.owner_external_user_id,
                    job_id=event.job_id,
                    worker_id=event.worker_id,
                    integration_id=integration.id,
                    event_type="mattermost.notification.sent",
                    source="integration",
                    message="Error notification delivered to Mattermost.",
                    details={"source_event_id": event.id},
                    queue_notification=False,
                )
            session.commit()
    return delivered


def transition_processing(
    session: Session,
    job: ProofJob,
    target: str,
    *,
    source: str,
    message: str,
    worker_id: str | None = None,
    details: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    allowed = ALLOWED_PROCESSING_TRANSITIONS.get(job.processing_status, set())
    if target not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot transition processing from {job.processing_status} to {target}.",
        )
    job.processing_status = target
    job.updated_at = utc_now()
    add_event(
        session,
        owner_external_user_id=job.owner_external_user_id,
        job_id=job.id,
        worker_id=worker_id or job.worker_id,
        event_type=f"processing.{target}",
        source=source,
        message=message,
        level="error" if target == "failed" else "info",
        details=details,
        error_code=error_code,
    )


def transition_delivery(
    session: Session,
    job: ProofJob,
    target: str,
    *,
    message: str,
    integration_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    allowed = ALLOWED_DELIVERY_TRANSITIONS.get(job.delivery_status, set())
    if target not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot transition delivery from {job.delivery_status} to {target}.",
        )
    job.delivery_status = target
    job.updated_at = utc_now()
    add_event(
        session,
        owner_external_user_id=job.owner_external_user_id,
        job_id=job.id,
        integration_id=integration_id,
        event_type=f"delivery.{target}",
        source="integration",
        message=message,
        level="error" if target == "failed" else "info",
        details=details,
    )


def latest_active_presets(session: Session, owner_external_user_id: int) -> list[ProofPreset]:
    rows = list(session.scalars(
        select(ProofPreset)
        .where(ProofPreset.owner_external_user_id == owner_external_user_id)
        .order_by(ProofPreset.logical_id, ProofPreset.version.desc())
    ))
    latest: dict[str, ProofPreset] = {}
    for row in rows:
        latest.setdefault(row.logical_id, row)
    return [row for row in latest.values() if row.is_active]


def create_preset(session: Session, owner_id: int, name: str, parameters: dict[str, Any]) -> ProofPreset:
    preset = ProofPreset(
        owner_external_user_id=owner_id,
        logical_id=str(uuid4()),
        name=name.strip()[:160],
        version=1,
        parameters_json=json_dump(parameters),
        is_active=True,
    )
    session.add(preset)
    session.flush()
    return preset


def revise_preset(session: Session, current: ProofPreset, name: str, parameters: dict[str, Any]) -> ProofPreset:
    current.is_active = False
    revised = ProofPreset(
        owner_external_user_id=current.owner_external_user_id,
        logical_id=current.logical_id,
        name=name.strip()[:160],
        version=current.version + 1,
        parameters_json=json_dump(parameters),
        is_active=True,
    )
    session.add(revised)
    session.flush()
    for integration in session.scalars(
        select(ProofIntegration).where(ProofIntegration.default_preset_id == current.id)
    ):
        integration.default_preset_id = revised.id
    return revised


def register_worker(session: Session, owner_id: int, name: str, heartbeat_timeout_seconds: int) -> tuple[ProofWorker, str]:
    if heartbeat_timeout_seconds < 1:
        raise ValueError("Heartbeat timeout must be greater than zero.")
    raw_token = new_secret("proof_worker")
    prefix, last_four = secret_parts(raw_token)
    worker = ProofWorker(
        id=str(uuid4()),
        owner_external_user_id=owner_id,
        name=name.strip()[:160],
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        configuration_json=json_dump(WORKER_RUNTIME_DEFAULTS),
        token_digest=token_digest(raw_token),
        token_prefix=prefix,
        token_last_four=last_four,
    )
    session.add(worker)
    session.flush()
    add_event(
        session,
        owner_external_user_id=owner_id,
        worker_id=worker.id,
        event_type="worker.registered",
        source="core",
        message=f"Worker {worker.name} registered.",
    )
    return worker, raw_token


def worker_configuration(worker: ProofWorker) -> dict[str, Any]:
    stored = json_load(worker.configuration_json, {})
    return {**WORKER_RUNTIME_DEFAULTS, **stored} if isinstance(stored, dict) else dict(WORKER_RUNTIME_DEFAULTS)


def update_worker_configuration(
    worker: ProofWorker,
    *,
    callback_url: str,
    unc_prefix: str,
    local_root: str,
    heartbeat_interval: float,
    poll_interval: float,
    retry_initial_seconds: float,
    retry_max_seconds: float,
    storage_retry_limit: int,
    file_not_found_retry_limit: int,
    health_interval: float,
    health_max_age: float,
    wake_timeout_seconds: float,
) -> None:
    if heartbeat_interval <= 0 or poll_interval <= 0 or retry_initial_seconds <= 0:
        raise ValueError("Интервалы Worker должны быть больше нуля.")
    if retry_max_seconds < retry_initial_seconds:
        raise ValueError("Максимальная задержка повтора не может быть меньше начальной.")
    if storage_retry_limit < 0 or file_not_found_retry_limit < 0:
        raise ValueError("Количество повторов не может быть отрицательным.")
    if health_interval <= 0 or health_max_age <= health_interval:
        raise ValueError("Health max age должен быть больше интервала healthcheck.")
    if wake_timeout_seconds <= 0:
        raise ValueError("Timeout webhook Worker должен быть больше нуля.")
    mappings = []
    normalized_prefix = unc_prefix.strip().rstrip("\\/")
    normalized_root = local_root.strip().rstrip("/")
    if normalized_prefix or normalized_root:
        if not normalized_prefix.startswith("\\\\"):
            raise ValueError("UNC-префикс должен начинаться с \\\\.")
        if not normalized_root.startswith("/"):
            raise ValueError("Путь mount внутри Worker должен быть абсолютным Linux-путём.")
        mappings.append({"source_prefix": normalized_prefix, "local_root": normalized_root})
    worker.callback_url = callback_url.strip()[:1000]
    worker.configuration_json = json_dump({
        "heartbeat_interval": heartbeat_interval,
        "poll_interval": poll_interval,
        "retry_initial_seconds": retry_initial_seconds,
        "retry_max_seconds": retry_max_seconds,
        "storage_retry_limit": storage_retry_limit,
        "file_not_found_retry_limit": file_not_found_retry_limit,
        "health_interval": health_interval,
        "health_max_age": health_max_age,
        "wake_timeout_seconds": wake_timeout_seconds,
        "path_mappings": mappings,
    })
    worker.configuration_version += 1
    worker.updated_at = utc_now()


def dispatch_worker_wakeups(
    session_factory: sessionmaker[Session],
    owner_id: int,
    job_id: str,
    *,
    client: httpx.Client | None = None,
) -> int:
    sent = 0
    owns_client = client is None
    http_client = client or httpx.Client()
    try:
        with session_factory() as session:
            workers = list(session.scalars(select(ProofWorker).where(
                ProofWorker.owner_external_user_id == owner_id,
                ProofWorker.callback_url != "",
            )))
            for worker in workers:
                configuration = worker_configuration(worker)
                timeout = configuration.get("wake_timeout_seconds")
                if not isinstance(timeout, (int, float)) or timeout <= 0:
                    continue
                timestamp = str(int(time.time()))
                body = json_dump({"event": "queue.changed", "job_id": job_id}).encode("utf-8")
                signature = hmac.new(
                    bytes.fromhex(worker.token_digest),
                    timestamp.encode("ascii") + b"." + body,
                    hashlib.sha256,
                ).hexdigest()
                try:
                    response = http_client.post(
                        worker.callback_url,
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-Proof-Timestamp": timestamp,
                            "X-Proof-Signature": signature,
                        },
                        timeout=float(timeout),
                    )
                    response.raise_for_status()
                    sent += 1
                except (httpx.HTTPError, ValueError) as exc:
                    add_event(
                        session,
                        owner_external_user_id=owner_id,
                        worker_id=worker.id,
                        job_id=job_id,
                        event_type="worker.wake.failed",
                        source="core",
                        level="warning",
                        message=f"Worker {worker.name} wake webhook failed; polling remains active.",
                        details={"error": sanitized_message(str(exc))},
                        queue_notification=False,
                    )
            session.commit()
    finally:
        if owns_client:
            http_client.close()
    return sent


def authenticate_worker(session: Session, raw_token: str) -> ProofWorker | None:
    if not raw_token:
        return None
    digest = token_digest(raw_token)
    worker = session.scalar(select(ProofWorker).where(ProofWorker.token_digest == digest))
    return worker if worker and constant_time_matches(raw_token, worker.token_digest) else None


def worker_is_online(worker: ProofWorker, now=None) -> bool:
    if worker.last_heartbeat_at is None:
        return False
    current = now or utc_now()
    return current - worker.last_heartbeat_at <= timedelta(seconds=worker.heartbeat_timeout_seconds)


def release_worker(worker: ProofWorker) -> None:
    worker.current_job_id = None
    if worker.availability != "error":
        worker.availability = "available"
    worker.updated_at = utc_now()


def requeue_stale_jobs(session: Session, owner_id: int | None = None) -> int:
    query = select(ProofWorker).where(ProofWorker.current_job_id.is_not(None))
    if owner_id is not None:
        query = query.where(ProofWorker.owner_external_user_id == owner_id)
    count = 0
    now = utc_now()
    for worker in session.scalars(query):
        if worker_is_online(worker, now):
            continue
        job = session.get(ProofJob, worker.current_job_id)
        if job and job.processing_status in {"assigned", "running"}:
            transition_processing(
                session,
                job,
                "queued",
                source="core",
                message=f"Worker {worker.name} heartbeat expired; Job returned to queue.",
                worker_id=worker.id,
                details={"last_heartbeat_at": worker.last_heartbeat_at},
            )
            job.worker_id = None
            job.assigned_at = None
            job.queued_at = now
            job.progress = None
            job.current_stage = ""
            count += 1
        release_worker(worker)
    return count


def create_job_from_webhook(
    session: Session,
    integration: ProofIntegration,
    *,
    idempotency_key: str,
    event_type: str,
    crm_entity_type: str,
    crm_entity_id: str,
    crm_order_id: str,
    preset: ProofPreset,
    input_payload: dict[str, Any],
    original_payload: dict[str, Any],
    accepted_override: bool | None = None,
    ignored_message: str | None = None,
) -> tuple[ProofJob | None, bool, bool]:
    # SQLite has no row-level locks. Upgrade the transaction before checking the
    # receipt so two simultaneous deliveries cannot both pass the lookup.
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    existing = session.scalar(select(WebhookReceipt).where(
        WebhookReceipt.integration_id == integration.id,
        WebhookReceipt.idempotency_key == idempotency_key,
    ))
    if existing:
        return session.get(ProofJob, existing.job_id) if existing.job_id else None, True, existing.accepted

    accepted_events = set(json_load(integration.trigger_events_json, []))
    accepted = event_type in accepted_events
    if accepted_override is not None:
        accepted = accepted and accepted_override
    receipt = WebhookReceipt(
        id=str(uuid4()), integration_id=integration.id, idempotency_key=idempotency_key,
        event_type=event_type, payload_json=json_dump(sanitized_details(original_payload)), accepted=accepted,
    )
    session.add(receipt)
    integration.last_incoming_at = utc_now()
    if not accepted:
        add_event(
            session, owner_external_user_id=integration.owner_external_user_id,
            integration_id=integration.id, event_type="webhook.ignored", source="integration",
            message=ignored_message or f"Webhook event {event_type} ignored by integration filter.",
        )
        session.flush()
        return None, False, False

    job = ProofJob(
        id=str(uuid4()), owner_external_user_id=integration.owner_external_user_id,
        source="amocrm", external_event_id=idempotency_key,
        crm_entity_type=crm_entity_type, crm_entity_id=crm_entity_id,
        crm_order_id=crm_order_id, input_json=json_dump(input_payload),
        processing_status="received", delivery_status="pending",
        preset_id=preset.id, preset_logical_id=preset.logical_id, preset_name=preset.name,
        preset_version=preset.version, preset_snapshot_json=preset.parameters_json,
    )
    session.add(job)
    session.flush()
    receipt.job_id = job.id
    add_event(
        session, owner_external_user_id=job.owner_external_user_id, job_id=job.id,
        integration_id=integration.id, event_type="webhook.received", source="integration",
        message="amoCRM webhook received and linked to Job.",
        details={"event_type": event_type, "crm_entity_type": crm_entity_type, "crm_entity_id": crm_entity_id},
    )
    transition_processing(session, job, "queued", source="core", message="Job added to processing queue.")
    job.queued_at = utc_now()
    return job, False, True


def claim_next_job(session: Session, worker: ProofWorker) -> ProofJob | None:
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    worker.last_heartbeat_at = utc_now()
    requeue_stale_jobs(session, worker.owner_external_user_id)
    session.refresh(worker)
    if worker.current_job_id:
        current = session.get(ProofJob, worker.current_job_id)
        if current and current.processing_status in {"assigned", "running"}:
            return current
        release_worker(worker)
    job = session.scalar(
        select(ProofJob)
        .where(
            ProofJob.owner_external_user_id == worker.owner_external_user_id,
            ProofJob.processing_status == "queued",
        )
        .order_by(ProofJob.queued_at, ProofJob.created_at)
        .limit(1)
    )
    if job is None:
        return None
    transition_processing(
        session, job, "assigned", source="worker", worker_id=worker.id,
        message=f"Job assigned to Worker {worker.name}.",
    )
    job.worker_id = worker.id
    job.assigned_at = utc_now()
    worker.current_job_id = job.id
    worker.availability = "busy"
    worker.updated_at = utc_now()
    session.flush()
    return job


def require_owned_job(session: Session, worker: ProofWorker, job_id: str) -> ProofJob:
    job = session.get(ProofJob, job_id)
    if job is None or job.owner_external_user_id != worker.owner_external_user_id:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.worker_id != worker.id:
        raise HTTPException(status_code=409, detail="Job is not assigned to this Worker.")
    return job


def safe_filename(filename: str | None) -> str:
    raw = Path(filename or "result.bin").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    return cleaned[:200] or "result.bin"


async def store_result(
    session: Session,
    settings: AppSettings,
    worker: ProofWorker,
    job: ProofJob,
    upload: UploadFile,
    idempotency_key: str,
    metadata: dict[str, Any],
) -> tuple[ProofResult, bool]:
    # Serialize the idempotency/attempt checks with the insert on SQLite. This
    # also prevents a losing concurrent request from writing an orphan file.
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    existing = session.scalar(select(ProofResult).where(
        ProofResult.worker_id == worker.id,
        ProofResult.idempotency_key == idempotency_key,
    ))
    if existing:
        if existing.job_id != job.id:
            raise HTTPException(status_code=409, detail="Idempotency key already belongs to another Job.")
        return existing, True
    if job.processing_status not in {"assigned", "running"}:
        raise HTTPException(status_code=409, detail="Job is not accepting a result in its current state.")
    existing_attempt = session.scalar(select(ProofResult).where(
        ProofResult.job_id == job.id,
        ProofResult.attempt == job.attempt,
    ))
    if existing_attempt:
        raise HTTPException(status_code=409, detail="Current Job attempt already has a Result.")

    content = await upload.read()
    digest = hashlib.sha256(content).hexdigest()
    result_id = str(uuid4())
    filename = safe_filename(upload.filename)
    directory = settings.result_dir / str(job.owner_external_user_id) / job.id / str(job.attempt)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{result_id}-{filename}"
    path.write_bytes(content)
    result = ProofResult(
        id=result_id, owner_external_user_id=job.owner_external_user_id, job_id=job.id,
        worker_id=worker.id, preset_logical_id=job.preset_logical_id,
        preset_version=job.preset_version, attempt=job.attempt,
        idempotency_key=idempotency_key, filename=filename,
        content_type=(upload.content_type or "application/octet-stream")[:160],
        file_path=str(path.relative_to(settings.result_dir)), file_size=len(content),
        sha256=digest, metadata_json=json_dump(metadata),
    )
    session.add(result)
    session.flush()
    job.current_result_id = result.id
    add_event(
        session, owner_external_user_id=job.owner_external_user_id, job_id=job.id,
        worker_id=worker.id, event_type="result.created", source="worker",
        message=f"Result {filename} uploaded by Worker {worker.name}.",
        details={"result_id": result.id, "file_size": len(content), "sha256": digest},
    )
    return result, False


def retry_job(session: Session, job: ProofJob) -> None:
    if job.processing_status not in {"failed", "cancelled", "completed"}:
        raise HTTPException(status_code=409, detail="Job cannot be retried in its current state.")
    transition_processing(session, job, "retrying", source="ui", message="Job retry requested.")
    if job.delivery_status != "pending":
        transition_delivery(
            session, job, "pending",
            message="New processing attempt requires a new delivery.",
        )
    if job.worker:
        release_worker(job.worker)
    job.worker_id = None
    job.current_result_id = None
    job.progress = None
    job.current_stage = ""
    job.error_code = None
    job.error_message = None
    job.started_at = None
    job.completed_at = None
    job.cancelled_at = None
    job.attempt += 1
    transition_processing(session, job, "queued", source="core", message="Retry added to queue.")
    job.queued_at = utc_now()


def cancel_job(session: Session, job: ProofJob) -> None:
    if job.processing_status not in {"received", "queued", "assigned", "running", "retrying"}:
        raise HTTPException(status_code=409, detail="Job cannot be cancelled in its current state.")
    transition_processing(session, job, "cancelled", source="ui", message="Job cancelled by user.")
    job.cancelled_at = utc_now()
    if job.worker:
        release_worker(job.worker)


def result_for_job(session: Session, job: ProofJob) -> ProofResult:
    result = session.get(ProofResult, job.current_result_id) if job.current_result_id else None
    if result is None:
        raise HTTPException(status_code=409, detail="Job has no current Result.")
    return result


def _result_path(settings: AppSettings, result: ProofResult) -> Path:
    result_root = settings.result_dir.resolve()
    file_path = (result_root / result.file_path).resolve()
    if result_root not in file_path.parents or not file_path.is_file():
        raise RuntimeError("Result file is unavailable.")
    return file_path


def _delivery_record(
    session: Session,
    job: ProofJob,
    result: ProofResult,
    integration: ProofIntegration,
) -> ProofResultDelivery:
    delivery = session.scalar(select(ProofResultDelivery).where(
        ProofResultDelivery.result_id == result.id,
        ProofResultDelivery.integration_id == integration.id,
    ))
    if delivery is None:
        delivery = ProofResultDelivery(
            id=str(uuid4()),
            owner_external_user_id=job.owner_external_user_id,
            job_id=job.id,
            result_id=result.id,
            integration_id=integration.id,
            archive_filename=f"BellenneProof-{job.id}.zip",
        )
        session.add(delivery)
        session.flush()
    return delivery


def _result_archive(
    settings: AppSettings,
    result: ProofResult,
    delivery: ProofResultDelivery,
) -> Path:
    source_path = _result_path(settings, result)
    archive_path = source_path.parent / f"{result.id}-amocrm.zip"
    if not archive_path.is_file():
        temporary_path = source_path.parent / f".{archive_path.name}.{uuid4().hex}.tmp"
        try:
            info = zipfile.ZipInfo(result.filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            with zipfile.ZipFile(
                temporary_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                with source_path.open("rb") as source, archive.open(info, "w") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
            os.replace(temporary_path, archive_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    digest = hashlib.sha256()
    with archive_path.open("rb") as archive_file:
        for block in iter(lambda: archive_file.read(1024 * 1024), b""):
            digest.update(block)
    delivery.archive_sha256 = digest.hexdigest()
    return archive_path


def _deliver_directly_to_amocrm(
    session: Session,
    settings: AppSettings,
    job: ProofJob,
    result: ProofResult,
    integration: ProofIntegration,
    configuration: AmoIntegrationConfiguration,
    *,
    client: httpx.Client | None,
) -> None:
    if not configuration.api_base_url or configuration.api_timeout_seconds is None:
        raise RuntimeError("amoCRM API connection is not configured.")
    if configuration.completed_status_id is None:
        raise RuntimeError("The completed amoCRM lead status is not configured.")
    if len(configuration.clear_field_ids) != 2:
        raise RuntimeError("Exactly two amoCRM fields must be selected for clearing.")
    if job.crm_entity_type not in {"lead", "leads"} or not job.crm_entity_id.isdecimal():
        raise RuntimeError("Job is not linked to a valid amoCRM lead.")

    delivery = _delivery_record(session, job, result, integration)
    archive_path = _result_archive(settings, result, delivery)
    credentials = decrypt_secret(
        credential_cipher_for_settings(settings), integration.credentials_encrypted
    )
    with AmoClient(
        configuration.api_base_url,
        credentials.get("access_token", ""),
        timeout_seconds=configuration.api_timeout_seconds,
        client=client,
    ) as amo:
        if not delivery.amo_file_uuid or not delivery.amo_version_uuid:
            with archive_path.open("rb") as archive_file:
                uploaded = amo.upload_file(
                    archive_file,
                    file_name=delivery.archive_filename,
                    file_size=archive_path.stat().st_size,
                    content_type="application/zip",
                )
            delivery.amo_file_uuid = uploaded["uuid"]
            delivery.amo_version_uuid = uploaded["version_uuid"]
            add_event(
                session,
                owner_external_user_id=job.owner_external_user_id,
                job_id=job.id,
                integration_id=integration.id,
                event_type="amocrm.archive.uploaded",
                source="integration",
                message="Result archive uploaded to amoCRM file storage.",
                details={
                    "result_id": result.id,
                    "archive_sha256": delivery.archive_sha256,
                    "file_uuid": delivery.amo_file_uuid,
                },
            )
            session.commit()

        if delivery.amo_note_id is None:
            note = amo.find_attachment_note(job.crm_entity_id, delivery.amo_file_uuid)
            if note is None:
                note = amo.create_attachment_note(
                    job.crm_entity_id,
                    file_uuid=delivery.amo_file_uuid,
                    version_uuid=delivery.amo_version_uuid,
                    file_name=delivery.archive_filename,
                )
            try:
                delivery.amo_note_id = int(note["id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("amoCRM did not return the attachment note ID.") from exc
            add_event(
                session,
                owner_external_user_id=job.owner_external_user_id,
                job_id=job.id,
                integration_id=integration.id,
                event_type="amocrm.archive.attached",
                source="integration",
                message="Result archive attached to the amoCRM lead.",
                details={"result_id": result.id, "note_id": delivery.amo_note_id},
            )
            session.commit()

        if delivery.finalized_at is None:
            status_payload: dict[str, Any] = {
                "status_id": configuration.completed_status_id,
                "custom_fields_values": [
                    {"field_id": field_id, "values": None}
                    for field_id in configuration.clear_field_ids
                ],
            }
            matching_status = next(
                (
                    item
                    for item in configuration.statuses_cache
                    if item.get("id") == configuration.completed_status_id
                ),
                None,
            )
            if matching_status and matching_status.get("pipeline_id"):
                status_payload["pipeline_id"] = matching_status["pipeline_id"]
            amo.update_lead(job.crm_entity_id, status_payload)
            delivery.finalized_at = utc_now()
            add_event(
                session,
                owner_external_user_id=job.owner_external_user_id,
                job_id=job.id,
                integration_id=integration.id,
                event_type="amocrm.lead.finalized",
                source="integration",
                message="amoCRM lead fields were cleared and the completed status was applied.",
                details={
                    "status_id": configuration.completed_status_id,
                    "cleared_field_ids": configuration.clear_field_ids,
                    "note_id": delivery.amo_note_id,
                },
            )


def deliver_result(
    session: Session,
    settings: AppSettings,
    job: ProofJob,
    *,
    client: httpx.Client | None = None,
) -> bool:
    result = result_for_job(session, job)
    integration = session.scalar(select(ProofIntegration).where(
        ProofIntegration.owner_external_user_id == job.owner_external_user_id,
        ProofIntegration.kind == "amocrm",
    ))
    if integration is None or not integration.enabled:
        transition_delivery(
            session, job, "delivering", message="Delivery attempt started.",
            integration_id=integration.id if integration else None,
        )
        transition_delivery(
            session, job, "failed", message="amoCRM integration is not enabled.",
            integration_id=integration.id if integration else None,
        )
        return False
    if job.delivery_status != "delivering":
        transition_delivery(
            session,
            job,
            "delivering",
            message="Delivery to amoCRM started.",
            integration_id=integration.id,
        )
    try:
        configuration = AmoIntegrationConfiguration.model_validate(
            json_load(integration.configuration_json, {})
        )
    except Exception as exc:
        transition_delivery(
            session,
            job,
            "failed",
            message="Delivery to amoCRM failed.",
            integration_id=integration.id,
            details={"error": sanitized_message(str(exc))},
        )
        integration.last_error_at = utc_now()
        integration.last_error_message = sanitized_message(str(exc))[:2000]
        return False
    if configuration.delivery_mode == "amocrm_attachment":
        try:
            _deliver_directly_to_amocrm(
                session,
                settings,
                job,
                result,
                integration,
                configuration,
                client=client,
            )
        except Exception as exc:
            safe_error = sanitized_message(str(exc))
            transition_delivery(
                session,
                job,
                "failed",
                message="Delivery to amoCRM failed.",
                integration_id=integration.id,
                details={"error": safe_error},
            )
            integration.last_error_at = utc_now()
            integration.last_error_message = safe_error[:2000]
            return False
        transition_delivery(
            session,
            job,
            "delivered",
            message="Result archive delivered to the amoCRM lead.",
            integration_id=integration.id,
        )
        integration.last_delivery_at = utc_now()
        integration.last_error_message = None
        return True
    payload = {
        "job_id": job.id,
        "crm_entity_type": job.crm_entity_type,
        "crm_entity_id": job.crm_entity_id,
        "crm_order_id": job.crm_order_id,
        "result": {
            "id": result.id,
            "filename": result.filename,
            "content_type": result.content_type,
            "size": result.file_size,
            "sha256": result.sha256,
            "download_path": f"{settings.module_prefix}/results/{result.id}/download",
        },
    }
    try:
        if integration.delivery_url == "mock://delivered":
            response_status = 200
        elif integration.delivery_url:
            owns_client = client is None
            file_path = _result_path(settings, result)
            http_client = client or httpx.Client()
            try:
                with file_path.open("rb") as result_file:
                    response_status = http_client.post(
                        integration.delivery_url,
                        data={"payload_json": json_dump(payload)},
                        files={"file": (result.filename, result_file, result.content_type)},
                    ).status_code
            finally:
                if owns_client:
                    http_client.close()
        else:
            raise RuntimeError("Delivery URL is not configured.")
        if not 200 <= response_status < 300:
            raise RuntimeError(f"amoCRM delivery endpoint returned HTTP {response_status}.")
    except Exception as exc:
        transition_delivery(
            session, job, "failed", message="Delivery to amoCRM failed.", integration_id=integration.id,
            details={"error": str(exc)},
        )
        integration.last_error_at = utc_now()
        integration.last_error_message = str(exc)[:2000]
        return False
    transition_delivery(session, job, "delivered", message="Result delivered to amoCRM.", integration_id=integration.id)
    integration.last_delivery_at = utc_now()
    integration.last_error_message = None
    return True


def credential_cipher_for_settings(settings: AppSettings):
    from .security import credential_cipher
    return credential_cipher(settings.credentials_secret, settings.credentials_key)


def retry_delivery(session: Session, settings: AppSettings, job: ProofJob) -> bool:
    if job.processing_status != "completed" or job.delivery_status != "failed":
        raise HTTPException(status_code=409, detail="Delivery cannot be retried in its current state.")
    transition_delivery(session, job, "retrying", message="Delivery retry requested.")
    return deliver_result(session, settings, job)
