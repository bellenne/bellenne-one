from __future__ import annotations

import hmac
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import event as sqlalchemy_event, func, or_, select
from sqlalchemy.orm import Session

from .config import AppSettings
from .database import build_engine, build_session_factory, init_database
from .models import (
    ProofEvent,
    ProofIntegration,
    ProofJob,
    ProofPreset,
    ProofResult,
    ProofWorker,
    utc_now,
)
from .schemas import FailureRequest, HeartbeatRequest, ProgressRequest, WebhookRequest, WorkerEventRequest
from .security import (
    constant_time_matches,
    decrypt_secret,
    encrypt_secret,
    masked_secret,
    new_secret,
    secret_parts,
    token_digest,
)
from .services import (
    DELIVERY_LABELS,
    PROCESSING_LABELS,
    add_event,
    authenticate_worker,
    cancel_job,
    claim_next_job,
    create_job_from_webhook,
    create_preset,
    credential_cipher_for_settings,
    deliver_result,
    dispatch_pending_mattermost,
    json_dump,
    json_load,
    latest_active_presets,
    mattermost_settings,
    post_mattermost_message,
    register_worker,
    release_worker,
    require_owned_job,
    retry_delivery,
    retry_job,
    revise_preset,
    sanitized_message,
    store_result,
    transition_processing,
    worker_is_online,
)


APP_VERSION = "1.1.0"
settings = AppSettings.from_env()
engine = build_engine(settings)
session_factory = build_session_factory(engine)
templates = Jinja2Templates(directory="app/templates")
notification_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="proof-mattermost")


def schedule_notifications_after_commit(session: Session) -> None:
    if session.info.pop("proof_notification_pending", False) and settings.notification_async_enabled:
        notification_executor.submit(dispatch_pending_mattermost, session_factory, settings)


sqlalchemy_event.listen(session_factory.class_, "after_commit", schedule_notifications_after_commit)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database(engine)
    if settings.notification_async_enabled:
        notification_executor.submit(dispatch_pending_mattermost, session_factory, settings)
    yield
    engine.dispose()


app = FastAPI(
    title="BellenneProof Core",
    version=APP_VERSION,
    description="Production orchestration API for BellenneProof Workers and amoCRM.",
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def get_db() -> Session:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def parse_user_id(raw: str | None) -> int | None:
    try:
        value = int(raw or "")
    except ValueError:
        return None
    return value if value > 0 else None


def request_identity(request: Request) -> tuple[int | None, str]:
    user_id = parse_user_id(request.headers.get("X-Bellenne-User-Id"))
    username = unquote(request.headers.get("X-Bellenne-Username", "")).strip()
    return user_id, username or (f"Пользователь #{user_id}" if user_id else "API-клиент")


def require_ui_identity(request: Request) -> tuple[int, str]:
    user_id, username = request_identity(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Bellenne identity header is required.")
    return user_id, username


def verify_csrf(request: Request, submitted: str) -> None:
    expected = unquote(request.headers.get("X-Bellenne-Csrf-Token", ""))
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")


def worker_token_from_request(
    authorization: str | None = Header(default=None, alias="Authorization"),
    worker_token: str | None = Header(default=None, alias="X-Proof-Worker-Token"),
) -> str:
    if worker_token:
        return worker_token
    if authorization and authorization.casefold().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def require_worker(
    raw_token: str = Depends(worker_token_from_request),
    session: Session = Depends(get_db),
) -> ProofWorker:
    worker = authenticate_worker(session, raw_token)
    if worker is None:
        raise HTTPException(status_code=401, detail="Invalid or missing Worker token.")
    return worker


def shared_context(request: Request, *, active: str, title: str) -> dict[str, Any]:
    _, username = request_identity(request)
    return {
        "request": request,
        "active": active,
        "title": title,
        "username": username,
        "module_prefix": settings.module_prefix,
        "platform_csrf_token": unquote(request.headers.get("X-Bellenne-Csrf-Token", "")),
        "version": APP_VERSION,
        "processing_labels": PROCESSING_LABELS,
        "delivery_labels": DELIVERY_LABELS,
        "json_load": json_load,
        "worker_is_online": worker_is_online,
    }


def render(request: Request, template: str, *, active: str, title: str, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={**shared_context(request, active=active, title=title), **context},
    )


def owned_job(session: Session, owner_id: int, job_id: str) -> ProofJob:
    job = session.get(ProofJob, job_id)
    if job is None or job.owner_external_user_id != owner_id:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


def worker_payload(worker: ProofWorker) -> dict[str, Any]:
    return {
        "id": worker.id,
        "name": worker.name,
        "hostname": worker.hostname,
        "online": worker_is_online(worker),
        "availability": worker.availability,
        "current_job_id": worker.current_job_id,
        "version": worker.version,
        "last_heartbeat_at": worker.last_heartbeat_at,
        "heartbeat_timeout_seconds": worker.heartbeat_timeout_seconds,
        "capabilities": json_load(worker.capabilities_json, []),
    }


def job_payload(job: ProofJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "source": job.source,
        "external_event_id": job.external_event_id,
        "crm_entity_type": job.crm_entity_type,
        "crm_entity_id": job.crm_entity_id,
        "crm_order_id": job.crm_order_id,
        "processing_status": job.processing_status,
        "delivery_status": job.delivery_status,
        "progress": job.progress,
        "current_stage": job.current_stage,
        "attempt": job.attempt,
        "input": json_load(job.input_json, {}),
        "preset": {
            "id": job.preset_logical_id,
            "name": job.preset_name,
            "version": job.preset_version,
            "parameters": json_load(job.preset_snapshot_json, {}),
        },
        "created_at": job.created_at,
        "queued_at": job.queued_at,
        "assigned_at": job.assigned_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }


def parse_amocrm_form_payload(raw_payload: dict[str, Any]) -> WebhookRequest:
    entity_type = ""
    action = ""
    entity_id = ""
    event_time = ""
    for key, value in raw_payload.items():
        match = re.match(r"^([a-z_]+)\[([a-z_]+)]\[\d+]\[([a-z_]+)]$", key)
        if not match:
            continue
        candidate_entity, candidate_action, field = match.groups()
        if field == "id" and not entity_id:
            entity_type, action, entity_id = candidate_entity, candidate_action, str(value)
        if field in {"datetime", "last_modified", "updated_at"} and not event_time:
            event_time = str(value)
    if not entity_type or not action or not entity_id:
        raise ValueError("Unsupported amoCRM form payload: entity, action, and id were not found.")
    account_id = str(raw_payload.get("account[id]", ""))
    idempotency_source = "|".join((account_id, entity_type, action, entity_id, event_time))
    event_id = hashlib.sha256(idempotency_source.encode("utf-8")).hexdigest()
    return WebhookRequest(
        event_id=event_id,
        event_type=f"{entity_type}.{action}",
        crm_entity_type=entity_type,
        crm_entity_id=entity_id,
        crm_order_id=str(raw_payload.get("crm_order_id", "")),
        preset_id=None,
        input=raw_payload,
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "BellenneProof Core"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/ui/state")
def ui_state(request: Request, session: Session = Depends(get_db)) -> dict[str, str]:
    owner_id, _ = require_ui_identity(request)
    values = [
        session.scalar(select(func.max(ProofJob.updated_at)).where(ProofJob.owner_external_user_id == owner_id)),
        session.scalar(select(func.max(ProofWorker.updated_at)).where(ProofWorker.owner_external_user_id == owner_id)),
        session.scalar(select(func.max(ProofResult.created_at)).where(ProofResult.owner_external_user_id == owner_id)),
        session.scalar(select(func.max(ProofEvent.created_at)).where(ProofEvent.owner_external_user_id == owner_id)),
    ]
    latest = max((value for value in values if value is not None), default=None)
    return {"version": latest.isoformat() if latest else "empty"}


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    jobs = list(session.scalars(select(ProofJob).where(
        ProofJob.owner_external_user_id == owner_id
    ).order_by(ProofJob.created_at.desc()).limit(8)))
    workers = list(session.scalars(select(ProofWorker).where(
        ProofWorker.owner_external_user_id == owner_id
    ).order_by(ProofWorker.name)))
    recent_errors = list(session.scalars(select(ProofEvent).where(
        ProofEvent.owner_external_user_id == owner_id,
        ProofEvent.level.in_(("warning", "error", "critical")),
    ).order_by(ProofEvent.created_at.desc()).limit(8)))
    recent_events = list(session.scalars(select(ProofEvent).where(
        ProofEvent.owner_external_user_id == owner_id,
    ).order_by(ProofEvent.created_at.desc()).limit(8)))
    counts = dict(session.execute(
        select(ProofJob.processing_status, func.count()).where(
            ProofJob.owner_external_user_id == owner_id
        ).group_by(ProofJob.processing_status)
    ).all())
    failed_jobs = session.scalar(select(func.count()).select_from(ProofJob).where(
        ProofJob.owner_external_user_id == owner_id,
        or_(ProofJob.processing_status == "failed", ProofJob.delivery_status == "failed"),
    )) or 0
    return render(
        request, "overview.html", active="overview", title="Обзор",
        jobs=jobs, workers=workers, recent_errors=recent_errors, recent_events=recent_events,
        counts=counts, failed_jobs=failed_jobs,
        online_workers=sum(worker_is_online(worker) for worker in workers),
    )


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    q: str = "",
    processing_status: str = "",
    delivery_status: str = "",
    worker_id: str = "",
    preset_id: str = "",
    source: str = "",
    session: Session = Depends(get_db),
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    query = select(ProofJob).where(ProofJob.owner_external_user_id == owner_id)
    if q:
        query = query.where(or_(
            ProofJob.id.contains(q), ProofJob.crm_entity_id.contains(q),
            ProofJob.crm_order_id.contains(q), ProofJob.external_event_id.contains(q),
        ))
    if processing_status:
        query = query.where(ProofJob.processing_status == processing_status)
    if delivery_status:
        query = query.where(ProofJob.delivery_status == delivery_status)
    if worker_id:
        query = query.where(ProofJob.worker_id == worker_id)
    if preset_id:
        query = query.where(ProofJob.preset_logical_id == preset_id)
    if source:
        query = query.where(ProofJob.source == source)
    jobs = list(session.scalars(query.order_by(ProofJob.created_at.desc()).limit(250)))
    workers = list(session.scalars(select(ProofWorker).where(ProofWorker.owner_external_user_id == owner_id)))
    return render(
        request, "jobs.html", active="jobs", title="Задания", jobs=jobs, workers=workers,
        presets=latest_active_presets(session, owner_id),
        filters={"q": q, "processing_status": processing_status, "delivery_status": delivery_status,
                 "worker_id": worker_id, "preset_id": preset_id, "source": source},
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str, request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    job = owned_job(session, owner_id, job_id)
    events = list(session.scalars(select(ProofEvent).where(
        ProofEvent.owner_external_user_id == owner_id, ProofEvent.job_id == job.id
    ).order_by(ProofEvent.created_at)))
    results = list(session.scalars(select(ProofResult).where(
        ProofResult.owner_external_user_id == owner_id, ProofResult.job_id == job.id
    ).order_by(ProofResult.created_at.desc())))
    return render(
        request, "job_detail.html", active="jobs", title=f"Job {job.id[:8]}",
        job=job, events=events, results=results,
        input_pretty=json.dumps(json_load(job.input_json, {}), ensure_ascii=False, indent=2),
        preset_pretty=json.dumps(json_load(job.preset_snapshot_json, {}), ensure_ascii=False, indent=2),
    )


@app.post("/jobs/{job_id}/retry")
def retry_job_ui(job_id: str, request: Request, csrf_token: str = Form(...), session: Session = Depends(get_db)):
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    retry_job(session, owned_job(session, owner_id, job_id))
    session.commit()
    return RedirectResponse(f"{settings.module_prefix}/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/retry-delivery")
def retry_delivery_ui(job_id: str, request: Request, csrf_token: str = Form(...), session: Session = Depends(get_db)):
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    retry_delivery(session, settings, owned_job(session, owner_id, job_id))
    session.commit()
    return RedirectResponse(f"{settings.module_prefix}/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/cancel")
def cancel_job_ui(job_id: str, request: Request, csrf_token: str = Form(...), session: Session = Depends(get_db)):
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    cancel_job(session, owned_job(session, owner_id, job_id))
    session.commit()
    return RedirectResponse(f"{settings.module_prefix}/jobs/{job_id}", status_code=303)


@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    jobs = list(session.scalars(select(ProofJob).where(
        ProofJob.owner_external_user_id == owner_id,
        ProofJob.processing_status.in_(("queued", "assigned", "running", "retrying")),
    ).order_by(ProofJob.queued_at, ProofJob.created_at)))
    workers = list(session.scalars(select(ProofWorker).where(
        ProofWorker.owner_external_user_id == owner_id,
    )))
    available_workers = sum(
        worker_is_online(worker) and worker.availability == "available" for worker in workers
    )
    waiting_jobs = sum(job.processing_status in {"queued", "retrying"} for job in jobs)
    return render(
        request, "queue.html", active="queue", title="Очередь", jobs=jobs, now=utc_now(),
        waiting_jobs=waiting_jobs, available_workers=available_workers,
    )


@app.get("/workers", response_class=HTMLResponse)
def workers_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    workers = list(session.scalars(select(ProofWorker).where(
        ProofWorker.owner_external_user_id == owner_id
    ).order_by(ProofWorker.name)))
    generated_token = request.query_params.get("token", "")
    return render(
        request, "workers.html", active="workers", title="Workers", workers=workers,
        generated_token=generated_token,
    )


@app.post("/workers/register")
def register_worker_ui(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    heartbeat_timeout_seconds: int = Form(...),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    worker, raw_token = register_worker(session, owner_id, name, heartbeat_timeout_seconds)
    session.commit()
    response = render(
        request, "workers.html", active="workers", title="Workers",
        workers=list(session.scalars(select(ProofWorker).where(ProofWorker.owner_external_user_id == owner_id))),
        generated_token=raw_token, generated_worker=worker,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/workers/{worker_id}/rotate-token")
def rotate_worker_token_ui(
    worker_id: str, request: Request, csrf_token: str = Form(...), session: Session = Depends(get_db)
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    worker = session.get(ProofWorker, worker_id)
    if worker is None or worker.owner_external_user_id != owner_id:
        raise HTTPException(status_code=404, detail="Worker not found.")
    raw_token = new_secret("proof_worker")
    worker.token_digest = token_digest(raw_token)
    worker.token_prefix, worker.token_last_four = secret_parts(raw_token)
    add_event(session, owner_external_user_id=owner_id, worker_id=worker.id,
              event_type="worker.token_rotated", source="ui", message=f"Token rotated for Worker {worker.name}.")
    session.commit()
    response = render(
        request, "workers.html", active="workers", title="Workers",
        workers=list(session.scalars(select(ProofWorker).where(ProofWorker.owner_external_user_id == owner_id))),
        generated_token=raw_token, generated_worker=worker,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/results", response_class=HTMLResponse)
def results_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    results = list(session.scalars(select(ProofResult).where(
        ProofResult.owner_external_user_id == owner_id
    ).order_by(ProofResult.created_at.desc()).limit(250)))
    return render(request, "results.html", active="results", title="Результаты", results=results)


@app.get("/results/{result_id}/download")
def download_result(result_id: str, request: Request, session: Session = Depends(get_db)) -> FileResponse:
    owner_id, _ = require_ui_identity(request)
    result = session.get(ProofResult, result_id)
    if result is None or result.owner_external_user_id != owner_id:
        raise HTTPException(status_code=404, detail="Result not found.")
    path = (settings.result_dir / result.file_path).resolve()
    if settings.result_dir not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Result file not found.")
    return FileResponse(path, media_type=result.content_type, filename=result.filename)


@app.get("/results/{result_id}/preview")
def preview_result(result_id: str, request: Request, session: Session = Depends(get_db)) -> FileResponse:
    owner_id, _ = require_ui_identity(request)
    result = session.get(ProofResult, result_id)
    if result is None or result.owner_external_user_id != owner_id or not result.content_type.startswith("image/"):
        raise HTTPException(status_code=404, detail="Preview not found.")
    path = (settings.result_dir / result.file_path).resolve()
    if settings.result_dir not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Preview file not found.")
    return FileResponse(path, media_type=result.content_type)


@app.get("/presets", response_class=HTMLResponse)
def presets_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    presets = list(session.scalars(select(ProofPreset).where(
        ProofPreset.owner_external_user_id == owner_id
    ).order_by(ProofPreset.logical_id, ProofPreset.version.desc())))
    return render(request, "presets.html", active="presets", title="Presets", presets=presets, error="")


@app.post("/presets")
def create_preset_ui(
    request: Request, csrf_token: str = Form(...), name: str = Form(...),
    parameters_json: str = Form(...), session: Session = Depends(get_db),
):
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    try:
        parameters = json.loads(parameters_json)
        if not isinstance(parameters, dict):
            raise ValueError("Preset parameters must be a JSON object.")
    except (ValueError, json.JSONDecodeError) as exc:
        return render(
            request, "presets.html", active="presets", title="Presets",
            presets=latest_active_presets(session, owner_id), error=str(exc),
        )
    create_preset(session, owner_id, name, parameters)
    session.commit()
    return RedirectResponse(f"{settings.module_prefix}/presets", status_code=303)


@app.post("/presets/{preset_id}/revise")
def revise_preset_ui(
    preset_id: int, request: Request, csrf_token: str = Form(...), name: str = Form(...),
    parameters_json: str = Form(...), session: Session = Depends(get_db),
):
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    preset = session.get(ProofPreset, preset_id)
    if preset is None or preset.owner_external_user_id != owner_id or not preset.is_active:
        raise HTTPException(status_code=404, detail="Active preset not found.")
    try:
        parameters = json.loads(parameters_json)
        if not isinstance(parameters, dict):
            raise ValueError("Preset parameters must be a JSON object.")
    except (ValueError, json.JSONDecodeError) as exc:
        return render(
            request, "presets.html", active="presets", title="Presets",
            presets=latest_active_presets(session, owner_id), error=str(exc),
        )
    revise_preset(session, preset, name, parameters)
    session.commit()
    return RedirectResponse(f"{settings.module_prefix}/presets", status_code=303)


@app.get("/integrations", response_class=HTMLResponse)
def integrations_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    return render_integrations_page(request, session, owner_id)


def render_integrations_page(
    request: Request,
    session: Session,
    owner_id: int,
    *,
    webhook_secret: str = "",
    mattermost_notice: str = "",
    mattermost_error: str = "",
) -> HTMLResponse:
    integration = session.scalar(select(ProofIntegration).where(
        ProofIntegration.owner_external_user_id == owner_id,
        ProofIntegration.kind == "amocrm",
    ))
    mattermost = session.scalar(select(ProofIntegration).where(
        ProofIntegration.owner_external_user_id == owner_id,
        ProofIntegration.kind == "mattermost",
    ))
    mattermost_webhook_url, mattermost_channel = (
        mattermost_settings(mattermost, settings) if mattermost else ("", "")
    )
    response = render(
        request, "integrations.html", active="integrations", title="Интеграции",
        integration=integration, presets=latest_active_presets(session, owner_id),
        webhook_secret=webhook_secret,
        webhook_mask=(masked_secret(integration.webhook_secret_prefix, integration.webhook_secret_last_four)
                      if integration else "Не создан"),
        mattermost=mattermost,
        mattermost_channel=mattermost_channel,
        mattermost_webhook_saved=bool(mattermost_webhook_url),
        mattermost_notice=mattermost_notice,
        mattermost_error=mattermost_error,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def valid_webhook_url(raw_value: str) -> str:
    value = raw_value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Webhook URL должен быть полным адресом http:// или https://.")
    return value


@app.post("/integrations/amocrm")
def configure_amocrm_ui(
    request: Request,
    csrf_token: str = Form(...),
    trigger_events: str = Form(...),
    default_preset_id: int = Form(...),
    delivery_url: str = Form(...),
    access_token: str = Form(""),
    enabled: str | None = Form(None),
    rotate_webhook_secret: str | None = Form(None),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    preset = session.get(ProofPreset, default_preset_id)
    if preset is None or preset.owner_external_user_id != owner_id or not preset.is_active:
        raise HTTPException(status_code=422, detail="Select an active Proof Preset.")
    events = [item.strip() for item in trigger_events.split(",") if item.strip()]
    if not events:
        raise HTTPException(status_code=422, detail="At least one trigger event is required.")
    integration = session.scalar(select(ProofIntegration).where(
        ProofIntegration.owner_external_user_id == owner_id, ProofIntegration.kind == "amocrm"
    ))
    raw_secret = ""
    if integration is None:
        raw_secret = new_secret("proof_hook")
        prefix, last_four = secret_parts(raw_secret)
        integration = ProofIntegration(
            owner_external_user_id=owner_id, kind="amocrm",
            webhook_secret_digest=token_digest(raw_secret), webhook_secret_prefix=prefix,
            webhook_secret_last_four=last_four,
        )
        session.add(integration)
    elif rotate_webhook_secret == "on":
        raw_secret = new_secret("proof_hook")
        integration.webhook_secret_digest = token_digest(raw_secret)
        integration.webhook_secret_prefix, integration.webhook_secret_last_four = secret_parts(raw_secret)
    integration.enabled = enabled == "on"
    integration.trigger_events_json = json_dump(events)
    integration.default_preset_id = preset.id
    integration.delivery_url = delivery_url.strip()[:1000]
    if access_token:
        integration.credentials_encrypted = encrypt_secret(
            credential_cipher_for_settings(settings), {"access_token": access_token}
        )
    session.flush()
    add_event(
        session, owner_external_user_id=owner_id, integration_id=integration.id,
        event_type="integration.configured", source="ui", message="amoCRM integration configuration saved.",
    )
    session.commit()
    return render_integrations_page(
        request, session, owner_id, webhook_secret=raw_secret
    )


@app.post("/integrations/mattermost")
def configure_mattermost_ui(
    request: Request,
    csrf_token: str = Form(...),
    webhook_url: str = Form(""),
    channel: str = Form(""),
    enabled: str | None = Form(None),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    integration = session.scalar(select(ProofIntegration).where(
        ProofIntegration.owner_external_user_id == owner_id,
        ProofIntegration.kind == "mattermost",
    ))
    existing_url, _ = mattermost_settings(integration, settings) if integration else ("", "")
    try:
        configured_url = valid_webhook_url(webhook_url) if webhook_url.strip() else existing_url
    except ValueError as exc:
        return render_integrations_page(
            request, session, owner_id, mattermost_error=str(exc)
        )
    if not configured_url:
        return render_integrations_page(
            request, session, owner_id,
            mattermost_error="Укажите Incoming Webhook URL Mattermost.",
        )
    if integration is None:
        internal_secret = new_secret("proof_internal")
        prefix, last_four = secret_parts(internal_secret)
        integration = ProofIntegration(
            owner_external_user_id=owner_id,
            kind="mattermost",
            webhook_secret_digest=token_digest(internal_secret),
            webhook_secret_prefix=prefix,
            webhook_secret_last_four=last_four,
        )
        session.add(integration)
    if webhook_url.strip():
        integration.credentials_encrypted = encrypt_secret(
            credential_cipher_for_settings(settings), {"webhook_url": configured_url}
        )
    integration.enabled = enabled == "on"
    integration.trigger_events_json = json_dump({"channel": channel.strip()[:120]})
    integration.delivery_url = ""
    session.flush()
    add_event(
        session,
        owner_external_user_id=owner_id,
        integration_id=integration.id,
        event_type="mattermost.configured",
        source="ui",
        message="Mattermost error notifications configured.",
    )
    session.commit()
    return render_integrations_page(
        request, session, owner_id,
        mattermost_notice="Настройки Mattermost сохранены.",
    )


@app.post("/integrations/mattermost/test")
def test_mattermost_ui(
    request: Request,
    csrf_token: str = Form(...),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    integration = session.scalar(select(ProofIntegration).where(
        ProofIntegration.owner_external_user_id == owner_id,
        ProofIntegration.kind == "mattermost",
    ))
    configured_url, channel = (
        mattermost_settings(integration, settings) if integration else ("", "")
    )
    if not configured_url:
        return render_integrations_page(
            request,
            session,
            owner_id,
            mattermost_error="Сначала сохраните Incoming Webhook URL Mattermost.",
        )

    try:
        post_mattermost_message(
            settings,
            configured_url,
            channel,
            "#### BellenneProof\nТестовое уведомление доставлено. Интеграция Mattermost работает.",
        )
    except Exception as exc:
        error = sanitized_message(str(exc).replace(configured_url, "[REDACTED_URL]"))[:2000]
        integration.last_error_at = utc_now()
        integration.last_error_message = error
        add_event(
            session,
            owner_external_user_id=owner_id,
            integration_id=integration.id,
            event_type="mattermost.test.failed",
            source="integration",
            level="error",
            message="Mattermost test notification failed.",
            error_code="MATTERMOST_TEST_FAILED",
            details={"error": error},
            queue_notification=False,
        )
        session.commit()
        return render_integrations_page(
            request, session, owner_id, mattermost_error=error
        )

    integration.last_delivery_at = utc_now()
    integration.last_error_at = None
    integration.last_error_message = None
    add_event(
        session,
        owner_external_user_id=owner_id,
        integration_id=integration.id,
        event_type="mattermost.test.sent",
        source="integration",
        message="Mattermost test notification delivered.",
    )
    session.commit()
    return render_integrations_page(
        request,
        session,
        owner_id,
        mattermost_notice="Тестовое уведомление Mattermost доставлено.",
    )


@app.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request, job_id: str = "", worker_id: str = "", integration_id: str = "",
    source: str = "", level: str = "", date_from: str = "", date_to: str = "",
    session: Session = Depends(get_db),
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    query = select(ProofEvent).where(ProofEvent.owner_external_user_id == owner_id)
    if job_id:
        query = query.where(ProofEvent.job_id.contains(job_id))
    if worker_id:
        query = query.where(ProofEvent.worker_id == worker_id)
    if integration_id.isdigit():
        query = query.where(ProofEvent.integration_id == int(integration_id))
    if source:
        query = query.where(ProofEvent.source == source)
    if level:
        query = query.where(ProofEvent.level == level)
    if date_from:
        try:
            from_value = datetime.combine(date.fromisoformat(date_from), time.min)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid date_from value.") from exc
        query = query.where(ProofEvent.created_at >= from_value)
    if date_to:
        try:
            to_value = datetime.combine(date.fromisoformat(date_to) + timedelta(days=1), time.min)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid date_to value.") from exc
        query = query.where(ProofEvent.created_at < to_value)
    events = list(session.scalars(query.order_by(ProofEvent.created_at.desc()).limit(500)))
    workers = list(session.scalars(select(ProofWorker).where(ProofWorker.owner_external_user_id == owner_id)))
    integrations = list(session.scalars(select(ProofIntegration).where(ProofIntegration.owner_external_user_id == owner_id)))
    return render(
        request, "logs.html", active="logs", title="Логи", events=events,
        workers=workers, integrations=integrations,
        filters={"job_id": job_id, "worker_id": worker_id, "integration_id": integration_id,
                 "source": source, "level": level, "date_from": date_from, "date_to": date_to},
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    return render(
        request, "settings.html", active="settings", title="Настройки",
        workers_count=session.scalar(select(func.count()).select_from(ProofWorker).where(
            ProofWorker.owner_external_user_id == owner_id)) or 0,
        presets_count=len(latest_active_presets(session, owner_id)),
        integration=session.scalar(select(ProofIntegration).where(
            ProofIntegration.owner_external_user_id == owner_id,
            ProofIntegration.kind == "amocrm")),
        mattermost=session.scalar(select(ProofIntegration).where(
            ProofIntegration.owner_external_user_id == owner_id,
            ProofIntegration.kind == "mattermost")),
    )


@app.post("/webhooks/amocrm/{webhook_secret}")
async def amocrm_webhook(webhook_secret: str, request: Request, session: Session = Depends(get_db)) -> JSONResponse:
    integration = next((row for row in session.scalars(select(ProofIntegration).where(
        ProofIntegration.kind == "amocrm", ProofIntegration.enabled.is_(True)
    )) if constant_time_matches(webhook_secret, row.webhook_secret_digest)), None)
    if integration is None:
        raise HTTPException(status_code=401, detail="Invalid webhook credential.")
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        raw_payload = await request.json()
    else:
        form = await request.form()
        raw_payload = {key: value for key, value in form.multi_items()}
    try:
        payload = (
            WebhookRequest.model_validate(raw_payload)
            if "application/json" in content_type
            else parse_amocrm_form_payload(raw_payload)
        )
    except (ValidationError, ValueError) as exc:
        integration.last_incoming_at = utc_now()
        integration.last_error_at = utc_now()
        integration.last_error_message = "Webhook contract validation failed."
        add_event(
            session,
            owner_external_user_id=integration.owner_external_user_id,
            integration_id=integration.id,
            event_type="webhook.validation_failed",
            source="integration",
            level="error",
            message="amoCRM webhook contract validation failed.",
            error_code="WEBHOOK_VALIDATION_FAILED",
        )
        session.commit()
        detail = exc.errors() if isinstance(exc, ValidationError) else str(exc)
        raise HTTPException(status_code=422, detail=detail) from exc
    preset_id = payload.preset_id or integration.default_preset_id
    preset = session.get(ProofPreset, preset_id) if preset_id else None
    if preset is None or preset.owner_external_user_id != integration.owner_external_user_id:
        raise HTTPException(status_code=422, detail="Webhook has no valid Proof Preset.")
    job, duplicate, accepted = create_job_from_webhook(
        session, integration, idempotency_key=payload.event_id, event_type=payload.event_type,
        crm_entity_type=payload.crm_entity_type, crm_entity_id=payload.crm_entity_id,
        crm_order_id=payload.crm_order_id, preset=preset,
        input_payload=payload.input, original_payload=raw_payload,
    )
    session.commit()
    return JSONResponse(
        {"accepted": accepted, "duplicate": duplicate, "job_id": job.id if job else None},
        status_code=200 if duplicate or not accepted else 202,
    )


@app.post("/api/v1/workers/heartbeat")
def worker_heartbeat(
    payload: HeartbeatRequest,
    worker: ProofWorker = Depends(require_worker),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    previous_availability = worker.availability
    previous_error = (worker.last_error_code, worker.last_error_message)
    worker.hostname = payload.hostname
    worker.version = payload.version
    worker.availability = payload.availability
    worker.capabilities_json = json_dump(payload.capabilities)
    worker.last_heartbeat_at = utc_now()
    worker.last_error_code = payload.last_error_code
    worker.last_error_message = sanitized_message(payload.last_error_message or "") or None
    if payload.current_job_id != worker.current_job_id:
        raise HTTPException(status_code=409, detail="Reported Job does not match assigned Job.")
    current_error = (worker.last_error_code, worker.last_error_message)
    is_new_error = current_error != previous_error or (
        previous_availability != "error" and not any(previous_error)
    )
    if worker.availability == "error" and is_new_error:
        add_event(
            session,
            owner_external_user_id=worker.owner_external_user_id,
            job_id=worker.current_job_id,
            worker_id=worker.id,
            event_type="worker.error",
            source="worker",
            level="error",
            message=worker.last_error_message or f"Worker {worker.name} reported an error state.",
            error_code=worker.last_error_code,
        )
    session.commit()
    return worker_payload(worker)


@app.post("/api/v1/jobs/claim")
def worker_claim(
    worker: ProofWorker = Depends(require_worker), session: Session = Depends(get_db)
) -> JSONResponse:
    worker.last_heartbeat_at = utc_now()
    job = claim_next_job(session, worker)
    session.commit()
    if job is None:
        return Response(status_code=204)
    return JSONResponse(jsonable_encoder(job_payload(job)))


@app.post("/api/v1/jobs/{job_id}/start")
def worker_start(
    job_id: str, worker: ProofWorker = Depends(require_worker), session: Session = Depends(get_db)
) -> dict[str, Any]:
    job = require_owned_job(session, worker, job_id)
    if job.processing_status == "running":
        return job_payload(job)
    transition_processing(session, job, "running", source="worker", worker_id=worker.id,
                          message=f"Worker {worker.name} started processing.")
    job.started_at = utc_now()
    job.progress = 0
    worker.availability = "busy"
    worker.last_heartbeat_at = utc_now()
    session.commit()
    return job_payload(job)


@app.post("/api/v1/jobs/{job_id}/progress")
def worker_progress(
    job_id: str, payload: ProgressRequest,
    worker: ProofWorker = Depends(require_worker), session: Session = Depends(get_db),
) -> dict[str, Any]:
    job = require_owned_job(session, worker, job_id)
    if job.processing_status != "running":
        raise HTTPException(status_code=409, detail="Job is not running.")
    job.progress = payload.progress
    job.current_stage = payload.current_stage
    job.updated_at = utc_now()
    worker.last_heartbeat_at = utc_now()
    add_event(
        session, owner_external_user_id=job.owner_external_user_id, job_id=job.id,
        worker_id=worker.id, event_type="processing.progress", source="worker",
        message=f"Worker progress: {payload.progress}%.",
        details={"progress": payload.progress, "current_stage": payload.current_stage},
    )
    session.commit()
    return {"job_id": job.id, "progress": job.progress, "current_stage": job.current_stage}


@app.post("/api/v1/jobs/{job_id}/events")
def worker_event(
    job_id: str, payload: WorkerEventRequest,
    worker: ProofWorker = Depends(require_worker), session: Session = Depends(get_db),
) -> dict[str, str]:
    job = require_owned_job(session, worker, job_id)
    add_event(
        session, owner_external_user_id=job.owner_external_user_id, job_id=job.id,
        worker_id=worker.id, event_type=payload.event_type, source="worker",
        level=payload.level, message=payload.message, details=payload.details,
        error_code=payload.error_code,
    )
    worker.last_heartbeat_at = utc_now()
    session.commit()
    return {"status": "recorded"}


@app.post("/api/v1/jobs/{job_id}/result")
async def worker_result(
    job_id: str,
    file: UploadFile = File(...),
    metadata_json: str = Form("{}"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    worker: ProofWorker = Depends(require_worker),
    session: Session = Depends(get_db),
) -> JSONResponse:
    job = require_owned_job(session, worker, job_id)
    try:
        metadata = json.loads(metadata_json)
        if not isinstance(metadata, dict):
            raise ValueError
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="metadata_json must be a JSON object.") from exc
    result, duplicate = await store_result(
        session, settings, worker, job, file, idempotency_key[:128], metadata
    )
    worker.last_heartbeat_at = utc_now()
    session.commit()
    return JSONResponse(
        {"result_id": result.id, "duplicate": duplicate, "sha256": result.sha256},
        status_code=200 if duplicate else 201,
    )


@app.post("/api/v1/jobs/{job_id}/complete")
def worker_complete(
    job_id: str, worker: ProofWorker = Depends(require_worker), session: Session = Depends(get_db)
) -> dict[str, Any]:
    job = require_owned_job(session, worker, job_id)
    if job.processing_status == "completed":
        return job_payload(job)
    if not job.current_result_id:
        raise HTTPException(status_code=409, detail="Upload a Result before completing the Job.")
    transition_processing(session, job, "completed", source="worker", worker_id=worker.id,
                          message=f"Worker {worker.name} completed processing.")
    job.progress = 100
    job.completed_at = utc_now()
    release_worker(worker)
    deliver_result(session, settings, job)
    session.commit()
    return job_payload(job)


@app.post("/api/v1/jobs/{job_id}/fail")
def worker_fail(
    job_id: str, payload: FailureRequest,
    worker: ProofWorker = Depends(require_worker), session: Session = Depends(get_db),
) -> dict[str, Any]:
    job = require_owned_job(session, worker, job_id)
    if job.processing_status == "failed":
        return job_payload(job)
    if job.processing_status not in {"assigned", "running"}:
        raise HTTPException(status_code=409, detail="Job cannot fail in its current state.")
    transition_processing(
        session, job, "failed", source="worker", worker_id=worker.id,
        message=payload.message, error_code=payload.error_code, details=payload.details,
    )
    job.error_code = payload.error_code
    job.error_message = sanitized_message(payload.message)
    worker.last_error_code = payload.error_code
    worker.last_error_message = sanitized_message(payload.message)
    release_worker(worker)
    session.commit()
    return job_payload(job)
