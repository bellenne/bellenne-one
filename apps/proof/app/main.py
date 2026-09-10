from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .amocrm import (
    AmoClient,
    AmoFieldMapping,
    AmoIntegrationConfiguration,
    amocrm_authorization_url,
    build_job_input,
    custom_field_value,
    exchange_amocrm_oauth_token,
    normalize_amocrm_referer,
    parse_optional_id,
)
from .config import AppSettings
from .database import build_engine, build_session_factory, init_database
from .models import (
    ProofEvent,
    ProofIntegration,
    ProofJob,
    ProofPreset,
    ProofResult,
    ProofWorker,
    WebhookReceipt,
    utc_now,
)
from .schemas import (
    FailureRequest,
    HeartbeatRequest,
    ProgressRequest,
    WebhookRequest,
    WorkerEventRequest,
)
from .security import (
    constant_time_matches,
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
    amocrm_credentials,
    amocrm_is_connected,
    authenticate_worker,
    cancel_job,
    claim_next_job,
    create_job_from_webhook,
    create_preset,
    credential_cipher_for_settings,
    deliver_result,
    dispatch_pending_mattermost,
    dispatch_worker_wakeups,
    get_amocrm_access_token,
    json_dump,
    json_load,
    latest_active_presets,
    mattermost_settings,
    post_mattermost_message,
    queue_received_job,
    register_worker,
    release_worker,
    require_owned_job,
    retry_delivery,
    retry_job,
    revise_preset,
    sanitized_message,
    store_amocrm_token_set,
    store_result,
    transition_processing,
    update_worker_configuration,
    worker_configuration,
    worker_is_online,
)
from .yandex_disk import YandexDiskClient

APP_VERSION = "1.1.2"
settings = AppSettings.from_env()
engine = build_engine(settings)
session_factory = build_session_factory(engine)
templates = Jinja2Templates(directory="app/templates")
notification_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="proof-integrations")
webhook_logger = logging.getLogger("bellenne.proof.webhooks")


def schedule_notifications_after_commit(session: Session) -> None:
    if session.info.pop("proof_notification_pending", False) and settings.notification_async_enabled:
        notification_executor.submit(dispatch_pending_mattermost, session_factory, settings)
    wake_request = session.info.pop("proof_worker_wake_pending", None)
    if wake_request:
        notification_executor.submit(
            dispatch_worker_wakeups,
            session_factory,
            wake_request["owner_id"],
            wake_request["job_id"],
        )


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
        "configuration_version": worker.configuration_version,
        "configuration": worker_configuration(worker),
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
    for key, value in raw_payload.items():
        match = re.match(r"^([a-z_]+)\[([a-z_]+)]\[(\d+)]\[([a-z_]+)]$", key)
        if not match:
            continue
        candidate_entity, candidate_action, _candidate_index, field = match.groups()
        if field == "id" and not entity_id:
            entity_type, action, entity_id = (
                candidate_entity,
                candidate_action,
                str(value),
            )
    if not entity_type or not action or not entity_id:
        raise ValueError("Unsupported amoCRM form payload: entity, action, and id were not found.")
    idempotency_source = json.dumps(
        sorted((str(key), str(value)) for key, value in raw_payload.items()),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    event_id = hashlib.sha256(idempotency_source.encode("utf-8")).hexdigest()
    crm_order_id = str(
        raw_payload.get("crm_order_id")
        or raw_payload.get("order_number")
        or entity_id
    ).strip()
    return WebhookRequest(
        event_id=event_id,
        event_type=f"{entity_type}.{action}",
        crm_entity_type=entity_type,
        crm_entity_id=entity_id,
        crm_order_id=crm_order_id,
        preset_id=None,
        input=raw_payload,
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "BellenneProof Core", "version": APP_VERSION}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION}


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
    job = owned_job(session, owner_id, job_id)
    delivered = retry_delivery(session, settings, job)
    update_amocrm_after_delivery(session, job, delivered=delivered)
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
        worker_configs={worker.id: worker_configuration(worker) for worker in workers},
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
        worker_configs={row.id: worker_configuration(row) for row in session.scalars(
            select(ProofWorker).where(ProofWorker.owner_external_user_id == owner_id)
        )},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/workers/{worker_id}/settings")
def configure_worker_ui(
    worker_id: str,
    request: Request,
    csrf_token: str = Form(...),
    callback_url: str = Form(""),
    unc_prefix: str = Form(""),
    local_root: str = Form(""),
    heartbeat_interval: float = Form(...),
    poll_interval: float = Form(...),
    retry_initial_seconds: float = Form(...),
    retry_max_seconds: float = Form(...),
    storage_retry_limit: int = Form(...),
    file_not_found_retry_limit: int = Form(...),
    health_interval: float = Form(...),
    health_max_age: float = Form(...),
    wake_timeout_seconds: float = Form(...),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    worker = session.get(ProofWorker, worker_id)
    if worker is None or worker.owner_external_user_id != owner_id:
        raise HTTPException(status_code=404, detail="Worker not found.")
    if callback_url.strip():
        valid_webhook_url(callback_url)
    try:
        update_worker_configuration(
            worker,
            callback_url=callback_url,
            unc_prefix=unc_prefix,
            local_root=local_root,
            heartbeat_interval=heartbeat_interval,
            poll_interval=poll_interval,
            retry_initial_seconds=retry_initial_seconds,
            retry_max_seconds=retry_max_seconds,
            storage_retry_limit=storage_retry_limit,
            file_not_found_retry_limit=file_not_found_retry_limit,
            health_interval=health_interval,
            health_max_age=health_max_age,
            wake_timeout_seconds=wake_timeout_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    add_event(
        session,
        owner_external_user_id=owner_id,
        worker_id=worker.id,
        event_type="worker.configuration.updated",
        source="ui",
        message=f"Runtime configuration updated for Worker {worker.name}.",
        details={"configuration_version": worker.configuration_version},
    )
    session.commit()
    return RedirectResponse(f"{settings.module_prefix}/workers", status_code=303)


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
        worker_configs={row.id: worker_configuration(row) for row in session.scalars(
            select(ProofWorker).where(ProofWorker.owner_external_user_id == owner_id)
        )},
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
    notice = {
        "connected": "amoCRM успешно подключена.",
        "disconnected": "Авторизация amoCRM сброшена.",
    }.get(request.query_params.get("amocrm", ""), "")
    error = {
        "access_denied": "Доступ к amoCRM не был предоставлен.",
        "oauth_failed": "Не удалось завершить OAuth-авторизацию amoCRM.",
    }.get(request.query_params.get("amocrm_error", ""), "")
    return render_integrations_page(
        request, session, owner_id, integration_notice=notice, integration_error=error
    )


def render_integrations_page(
    request: Request,
    session: Session,
    owner_id: int,
    *,
    webhook_secret: str = "",
    mattermost_notice: str = "",
    mattermost_error: str = "",
    integration_notice: str = "",
    integration_error: str = "",
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
    try:
        amo_config = AmoIntegrationConfiguration.model_validate(
            json_load(integration.configuration_json, {}) if integration else {}
        )
    except ValidationError:
        amo_config = AmoIntegrationConfiguration()
    oauth_credentials = amocrm_credentials(integration, settings) if integration else {}
    oauth_connected = bool(integration and amocrm_is_connected(integration, settings))
    token_expires_at = oauth_credentials.get("expires_at")
    token_expires_at_display = "—"
    if isinstance(token_expires_at, (int, float)):
        token_expires_at_display = datetime.fromtimestamp(
            token_expires_at
        ).strftime("%d.%m.%Y %H:%M:%S")
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
        integration_notice=integration_notice,
        integration_error=integration_error,
        amo_config=amo_config,
        amo_fields=amo_config.fields_cache,
        amo_statuses=amo_config.statuses_cache,
        oauth_client_id=oauth_credentials.get("client_id", ""),
        oauth_client_secret_saved=bool(oauth_credentials.get("client_secret")),
        oauth_connected=oauth_connected,
        oauth_token_expires_at=token_expires_at_display,
        yandex_disk_token_saved=bool(oauth_credentials.get("yandex_disk_token")),
        amocrm_redirect_uri=settings.amocrm_redirect_uri if settings.public_base_url else "",
        amocrm_revoked_uri=settings.amocrm_revoked_uri if settings.public_base_url else "",
        amocrm_webhook_base=(
            f"{settings.public_base_url}{settings.module_prefix}/webhooks/amocrm/"
            if settings.public_base_url else f"{settings.module_prefix}/webhooks/amocrm/"
        ),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def valid_webhook_url(raw_value: str) -> str:
    value = raw_value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Webhook URL должен быть полным адресом http:// или https://.")
    return value


def owned_amocrm_integration(
    session: Session, owner_id: int, *, create: bool = False
) -> tuple[ProofIntegration | None, str]:
    integration = session.scalar(select(ProofIntegration).where(
        ProofIntegration.owner_external_user_id == owner_id,
        ProofIntegration.kind == "amocrm",
    ))
    raw_secret = ""
    if integration is None and create:
        raw_secret = new_secret("proof_hook")
        prefix, last_four = secret_parts(raw_secret)
        integration = ProofIntegration(
            owner_external_user_id=owner_id,
            kind="amocrm",
            enabled=False,
            trigger_events_json=json_dump(["leads.status"]),
            webhook_secret_digest=token_digest(raw_secret),
            webhook_secret_prefix=prefix,
            webhook_secret_last_four=last_four,
        )
        session.add(integration)
        session.flush()
    return integration, raw_secret


def require_amocrm_integration(session: Session, owner_id: int) -> ProofIntegration:
    integration, _ = owned_amocrm_integration(session, owner_id)
    if integration is None:
        raise HTTPException(status_code=409, detail="Сначала выполните шаг авторизации amoCRM.")
    return integration


@app.post("/integrations/amocrm/oauth/settings")
def configure_amocrm_oauth_ui(
    request: Request,
    csrf_token: str = Form(...),
    api_base_url: str = Form(...),
    api_timeout_seconds: float = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(""),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    if not settings.public_base_url or not settings.public_base_url.startswith("https://"):
        raise HTTPException(status_code=500, detail="PROOF_PUBLIC_BASE_URL должен быть HTTPS-адресом.")
    integration, raw_secret = owned_amocrm_integration(session, owner_id, create=True)
    assert integration is not None
    previous = AmoIntegrationConfiguration.model_validate(
        json_load(integration.configuration_json, {})
    )
    updated = AmoIntegrationConfiguration.model_validate({
        **previous.model_dump(mode="json"),
        "api_base_url": api_base_url,
        "api_timeout_seconds": api_timeout_seconds,
    })
    credentials = amocrm_credentials(integration, settings)
    normalized_client_id = client_id.strip()
    normalized_client_secret = client_secret.strip() or str(credentials.get("client_secret") or "")
    if not normalized_client_id or not normalized_client_secret:
        raise HTTPException(status_code=422, detail="Укажите Integration ID и Secret key amoCRM.")
    identity_changed = any((
        credentials.get("client_id") != normalized_client_id,
        credentials.get("client_secret") != normalized_client_secret,
        previous.api_base_url != updated.api_base_url,
    ))
    stored_credentials = {
        "client_id": normalized_client_id,
        "client_secret": normalized_client_secret,
    }
    if credentials.get("yandex_disk_token"):
        stored_credentials["yandex_disk_token"] = credentials["yandex_disk_token"]
    if not identity_changed:
        stored_credentials.update({
            key: credentials[key]
            for key in ("access_token", "refresh_token", "expires_at")
            if key in credentials
        })
    integration.credentials_encrypted = encrypt_secret(
        credential_cipher_for_settings(settings), stored_credentials
    )
    integration.configuration_json = json_dump(updated.model_dump(mode="json"))
    integration.enabled = integration.enabled and not identity_changed
    integration.oauth_state_digest = ""
    integration.oauth_state_expires_at = None
    add_event(
        session,
        owner_external_user_id=owner_id,
        integration_id=integration.id,
        event_type="amocrm.oauth.settings_updated",
        source="ui",
        message="amoCRM OAuth application settings saved.",
    )
    session.commit()
    return render_integrations_page(
        request,
        session,
        owner_id,
        webhook_secret=raw_secret,
        integration_notice="Параметры приложения amoCRM сохранены.",
    )


@app.post("/integrations/amocrm/webhook")
def configure_amocrm_webhook_ui(
    request: Request,
    csrf_token: str = Form(...),
    source_path_field_id: str = Form(...),
    layout_number_field_id: str = Form(...),
    third_field_id: str = Form(...),
    designer_field_id: str = Form(""),
    clear_source_path: str | None = Form(None),
    clear_layout_number: str | None = Form(None),
    rotate_webhook_secret: str | None = Form(None),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    integration = require_amocrm_integration(session, owner_id)
    configuration = AmoIntegrationConfiguration.model_validate(
        json_load(integration.configuration_json, {})
    )
    mappings = [
        AmoFieldMapping(target="source_path", field_id=parse_optional_id(source_path_field_id)),
        AmoFieldMapping(target="layout_number", field_id=parse_optional_id(layout_number_field_id)),
        AmoFieldMapping(target="public_id", field_id=parse_optional_id(third_field_id)),
    ]
    designer_field = parse_optional_id(designer_field_id)
    if designer_field is not None:
        mappings.append(AmoFieldMapping(target="designer_name", field_id=designer_field))
    clear_field_ids = [
        mapping.field_id
        for mapping, selected in zip(
            mappings,
            (clear_source_path, clear_layout_number, "on"),
            strict=True,
        )
        if selected == "on"
    ]
    if not clear_field_ids:
        raise HTTPException(status_code=422, detail="Выберите хотя бы одно поле для очистки.")
    configuration = AmoIntegrationConfiguration.model_validate({
        **configuration.model_dump(mode="json"),
        "mappings": [item.model_dump(mode="json") for item in mappings],
        "clear_field_ids": clear_field_ids,
    })
    raw_secret = ""
    if rotate_webhook_secret == "on":
        raw_secret = new_secret("proof_hook")
        integration.webhook_secret_digest = token_digest(raw_secret)
        integration.webhook_secret_prefix, integration.webhook_secret_last_four = secret_parts(raw_secret)
    integration.trigger_events_json = json_dump(["leads.status"])
    integration.configuration_json = json_dump(configuration.model_dump(mode="json"))
    add_event(
        session,
        owner_external_user_id=owner_id,
        integration_id=integration.id,
        event_type="amocrm.webhook.configured",
        source="ui",
        message="amoCRM webhook field mapping saved.",
        details={"mapped_fields": len(mappings), "cleared_fields": len(clear_field_ids)},
    )
    session.commit()
    return render_integrations_page(
        request, session, owner_id, webhook_secret=raw_secret,
        integration_notice="Поля webhook сохранены.",
    )


@app.post("/integrations/amocrm/statuses")
def configure_amocrm_statuses_ui(
    request: Request,
    csrf_token: str = Form(...),
    queued_status_id: str = Form(...),
    completed_status_id: str = Form(...),
    failed_status_id: str = Form(""),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    integration = require_amocrm_integration(session, owner_id)
    configuration = AmoIntegrationConfiguration.model_validate(
        json_load(integration.configuration_json, {})
    )
    configuration = AmoIntegrationConfiguration.model_validate({
        **configuration.model_dump(mode="json"),
        "queued_status_id": parse_optional_id(queued_status_id),
        "completed_status_id": parse_optional_id(completed_status_id),
        "failed_status_id": parse_optional_id(failed_status_id),
    })
    integration.configuration_json = json_dump(configuration.model_dump(mode="json"))
    add_event(
        session,
        owner_external_user_id=owner_id,
        integration_id=integration.id,
        event_type="amocrm.statuses.configured",
        source="ui",
        message="amoCRM lead statuses saved.",
    )
    session.commit()
    return render_integrations_page(
        request, session, owner_id, integration_notice="Статусы сделки сохранены."
    )


@app.post("/integrations/amocrm/yandex-disk")
def configure_yandex_disk_ui(
    request: Request,
    csrf_token: str = Form(...),
    oauth_token: str = Form(""),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    integration = require_amocrm_integration(session, owner_id)
    configuration = AmoIntegrationConfiguration.model_validate(
        json_load(integration.configuration_json, {})
    )
    credentials = amocrm_credentials(integration, settings)
    token = oauth_token.strip() or str(credentials.get("yandex_disk_token") or "")
    try:
        updated = AmoIntegrationConfiguration.model_validate({
            **configuration.model_dump(mode="json"),
            "delivery_mode": "yandex_disk_note",
        })
        if not token:
            raise ValueError("Укажите OAuth token Яндекс.Диска.")
        if updated.api_timeout_seconds is None:
            raise ValueError("Сначала настройте HTTP timeout подключения amoCRM.")
        with YandexDiskClient(
            token,
            timeout_seconds=updated.api_timeout_seconds,
        ) as disk:
            disk.account()
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        return render_integrations_page(
            request,
            session,
            owner_id,
            integration_error=sanitized_message(str(exc)),
        )
    credentials["yandex_disk_token"] = token
    integration.credentials_encrypted = encrypt_secret(
        credential_cipher_for_settings(settings), credentials
    )
    integration.configuration_json = json_dump(updated.model_dump(mode="json"))
    add_event(
        session,
        owner_external_user_id=owner_id,
        integration_id=integration.id,
        event_type="yandex_disk.configured",
        source="ui",
        message="Yandex Disk delivery settings saved.",
        details={"root_path": updated.yandex_disk_root},
    )
    session.commit()
    return render_integrations_page(
        request,
        session,
        owner_id,
        integration_notice="Яндекс.Диск подключён.",
    )


@app.post("/integrations/amocrm/execution")
def configure_amocrm_execution_ui(
    request: Request,
    csrf_token: str = Form(...),
    default_preset_id: int = Form(...),
    enabled: str | None = Form(None),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    integration = require_amocrm_integration(session, owner_id)
    preset = session.get(ProofPreset, default_preset_id)
    if preset is None or preset.owner_external_user_id != owner_id or not preset.is_active:
        raise HTTPException(status_code=422, detail="Выберите активный Proof Preset.")
    configuration = AmoIntegrationConfiguration.model_validate(
        json_load(integration.configuration_json, {})
    )
    should_enable = enabled == "on"
    if should_enable:
        if not amocrm_is_connected(integration, settings):
            raise HTTPException(status_code=422, detail="Сначала авторизуйте amoCRM.")
        if not configuration.has_required_mappings() or not configuration.clear_field_ids:
            raise HTTPException(status_code=422, detail="Настройте три поля и их очистку.")
        if configuration.queued_status_id is None or configuration.completed_status_id is None:
            raise HTTPException(status_code=422, detail="Настройте начальный и конечный статусы.")
        credentials = amocrm_credentials(integration, settings)
        if not configuration.yandex_disk_root or not credentials.get("yandex_disk_token"):
            raise HTTPException(status_code=422, detail="Сначала подключите Яндекс.Диск.")
    integration.default_preset_id = preset.id
    integration.enabled = should_enable
    integration.delivery_url = ""
    configuration.delivery_mode = "yandex_disk_note"
    integration.configuration_json = json_dump(configuration.model_dump(mode="json"))
    add_event(
        session,
        owner_external_user_id=owner_id,
        integration_id=integration.id,
        event_type="amocrm.execution.configured",
        source="ui",
        message="amoCRM execution settings saved.",
        details={"enabled": should_enable, "preset_id": preset.id},
    )
    session.commit()
    return render_integrations_page(
        request, session, owner_id,
        integration_notice="Интеграция включена." if should_enable else "Настройки выполнения сохранены.",
    )


@app.post("/integrations/amocrm/oauth/start")
def start_amocrm_oauth_ui(
    request: Request,
    csrf_token: str = Form(...),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    integration = require_amocrm_integration(session, owner_id)
    configuration = AmoIntegrationConfiguration.model_validate(
        json_load(integration.configuration_json, {})
    )
    credentials = amocrm_credentials(integration, settings)
    client_id = str(credentials.get("client_id") or "")
    if not client_id or not credentials.get("client_secret"):
        raise HTTPException(status_code=422, detail="Сначала сохраните Integration ID и Secret key.")
    if not configuration.api_base_url or configuration.api_timeout_seconds is None:
        raise HTTPException(status_code=422, detail="Сначала сохраните адрес аккаунта amoCRM.")
    if not settings.public_base_url.startswith("https://"):
        raise HTTPException(status_code=500, detail="PROOF_PUBLIC_BASE_URL должен быть HTTPS-адресом.")
    state_value = new_secret("proof_amo_oauth")
    integration.oauth_state_digest = token_digest(state_value)
    integration.oauth_state_expires_at = utc_now() + timedelta(minutes=20)
    session.commit()
    return RedirectResponse(
        amocrm_authorization_url(client_id, configuration.api_base_url, state_value),
        status_code=303,
    )


def pending_oauth_integration(session: Session, state_value: str) -> ProofIntegration | None:
    now = utc_now()
    return next((
        integration
        for integration in session.scalars(select(ProofIntegration).where(
            ProofIntegration.kind == "amocrm",
            ProofIntegration.oauth_state_expires_at.is_not(None),
            ProofIntegration.oauth_state_expires_at >= now,
        ))
        if constant_time_matches(state_value, integration.oauth_state_digest)
    ), None)


@app.get("/integrations/amocrm/oauth/callback")
def amocrm_oauth_callback(
    state: str = "",
    code: str = "",
    referer: str = "",
    error: str = "",
    session: Session = Depends(get_db),
) -> Response:
    if not any((state, code, referer, error)):
        return JSONResponse({"status": "ready"})
    integration = pending_oauth_integration(session, state)
    if integration is None:
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired.")
    integration.oauth_state_digest = ""
    integration.oauth_state_expires_at = None
    if error:
        integration.last_error_at = utc_now()
        integration.last_error_message = "amoCRM OAuth access was denied."
        session.commit()
        return RedirectResponse(
            f"{settings.module_prefix}/integrations?amocrm_error=access_denied",
            status_code=303,
        )
    try:
        configuration = AmoIntegrationConfiguration.model_validate(
            json_load(integration.configuration_json, {})
        )
        referer_url = normalize_amocrm_referer(referer)
        if referer_url != configuration.api_base_url:
            raise ValueError("amoCRM returned a different account domain.")
        credentials = amocrm_credentials(integration, settings)
        token_set = exchange_amocrm_oauth_token(
            configuration.api_base_url,
            client_id=str(credentials.get("client_id") or ""),
            client_secret=str(credentials.get("client_secret") or ""),
            redirect_uri=settings.amocrm_redirect_uri,
            grant_type="authorization_code",
            code=code,
            timeout_seconds=configuration.api_timeout_seconds or 0,
        )
        with AmoClient(
            configuration.api_base_url,
            token_set.access_token,
            timeout_seconds=configuration.api_timeout_seconds,
        ) as client:
            account = client.get_account()
        account_id = account.get("id")
        if not isinstance(account_id, int):
            raise RuntimeError("amoCRM account response has no valid ID.")
        store_amocrm_token_set(integration, settings, credentials, token_set)
        configuration.account_id = account_id
        configuration.account_name = str(account.get("name") or "")[:255]
        configuration.connected_at = utc_now().isoformat()
        integration.configuration_json = json_dump(configuration.model_dump(mode="json"))
        integration.last_error_message = None
        add_event(
            session,
            owner_external_user_id=integration.owner_external_user_id,
            integration_id=integration.id,
            event_type="amocrm.oauth.connected",
            source="integration",
            message="amoCRM OAuth connection established.",
            details={"account_id": account_id},
        )
        session.commit()
    except (ValueError, ValidationError, RuntimeError, httpx.HTTPError) as exc:
        integration.last_error_at = utc_now()
        integration.last_error_message = sanitized_message(str(exc))[:2000]
        session.commit()
        return RedirectResponse(
            f"{settings.module_prefix}/integrations?amocrm_error=oauth_failed",
            status_code=303,
        )
    return RedirectResponse(
        f"{settings.module_prefix}/integrations?amocrm=connected",
        status_code=303,
    )


@app.post("/integrations/amocrm/oauth/disconnect")
def disconnect_amocrm_oauth_ui(
    request: Request,
    csrf_token: str = Form(...),
    confirm_disconnect: str = Form(...),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    if confirm_disconnect != "on":
        raise HTTPException(status_code=422, detail="Подтвердите сброс авторизации.")
    integration = require_amocrm_integration(session, owner_id)
    credentials = amocrm_credentials(integration, settings)
    retained = {
        key: credentials[key]
        for key in ("client_id", "client_secret", "yandex_disk_token")
        if credentials.get(key)
    }
    integration.credentials_encrypted = encrypt_secret(
        credential_cipher_for_settings(settings), retained
    )
    integration.enabled = False
    integration.oauth_state_digest = ""
    integration.oauth_state_expires_at = None
    configuration = AmoIntegrationConfiguration.model_validate(
        json_load(integration.configuration_json, {})
    )
    configuration.account_id = None
    configuration.account_name = ""
    configuration.connected_at = ""
    integration.configuration_json = json_dump(configuration.model_dump(mode="json"))
    add_event(
        session,
        owner_external_user_id=owner_id,
        integration_id=integration.id,
        event_type="amocrm.oauth.disconnected",
        source="ui",
        message="amoCRM OAuth credentials cleared locally.",
    )
    session.commit()
    return RedirectResponse(
        f"{settings.module_prefix}/integrations?amocrm=disconnected",
        status_code=303,
    )


@app.get("/integrations/amocrm/oauth/revoked")
def amocrm_oauth_revoked(
    account_id: str = "",
    client_uuid: str = "",
    client_id: str = "",
    signature: str = "",
    session: Session = Depends(get_db),
) -> dict[str, str]:
    if not any((account_id, client_uuid, client_id, signature)):
        return {"status": "ready"}
    received_client_id = client_uuid or client_id
    matched: tuple[ProofIntegration, dict[str, Any]] | None = None
    for integration in session.scalars(select(ProofIntegration).where(
        ProofIntegration.kind == "amocrm"
    )):
        credentials = amocrm_credentials(integration, settings)
        try:
            configuration = amocrm_configuration(integration)
        except ValidationError:
            continue
        if (
            hmac.compare_digest(str(credentials.get("client_id") or ""), received_client_id)
            and str(configuration.account_id or "") == account_id
        ):
            matched = integration, credentials
            break
    if matched is None:
        raise HTTPException(status_code=401, detail="Invalid revocation hook.")
    integration, credentials = matched
    expected = hmac.new(
        str(credentials.get("client_secret") or "").encode("utf-8"),
        f"{received_client_id}|{account_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid revocation hook signature.")
    retained = {
        key: credentials[key]
        for key in ("client_id", "client_secret", "yandex_disk_token")
        if credentials.get(key)
    }
    integration.credentials_encrypted = encrypt_secret(
        credential_cipher_for_settings(settings), retained
    )
    integration.enabled = False
    integration.oauth_state_digest = ""
    integration.oauth_state_expires_at = None
    configuration.account_id = None
    configuration.account_name = ""
    configuration.connected_at = ""
    integration.configuration_json = json_dump(configuration.model_dump(mode="json"))
    add_event(
        session,
        owner_external_user_id=integration.owner_external_user_id,
        integration_id=integration.id,
        event_type="amocrm.oauth.revoked",
        source="integration",
        level="warning",
        message="amoCRM reported that integration access was revoked.",
        details={"account_id": account_id},
    )
    session.commit()
    return {"status": "accepted"}


@app.post("/integrations/amocrm/catalog")
def load_amocrm_catalog_ui(
    request: Request,
    csrf_token: str = Form(...),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    owner_id, _ = require_ui_identity(request)
    verify_csrf(request, csrf_token)
    integration = session.scalar(select(ProofIntegration).where(
        ProofIntegration.owner_external_user_id == owner_id,
        ProofIntegration.kind == "amocrm",
    ))
    if integration is None:
        return render_integrations_page(
            request, session, owner_id,
            integration_error="Сначала сохраните подключение amoCRM.",
        )
    try:
        configuration = AmoIntegrationConfiguration.model_validate(
            json_load(integration.configuration_json, {})
        )
        access_token = get_amocrm_access_token(session, settings, integration)
        with AmoClient(
            configuration.api_base_url,
            access_token,
            timeout_seconds=configuration.api_timeout_seconds,
        ) as client:
            fields, statuses = client.load_catalog()
    except (ValueError, ValidationError, RuntimeError, httpx.HTTPError) as exc:
        return render_integrations_page(
            request, session, owner_id,
            integration_error=sanitized_message(str(exc)),
        )
    configuration.fields_cache = fields
    configuration.statuses_cache = statuses
    integration.configuration_json = json_dump(configuration.model_dump(mode="json"))
    integration.last_incoming_at = utc_now()
    add_event(
        session,
        owner_external_user_id=owner_id,
        integration_id=integration.id,
        event_type="amocrm.catalog.loaded",
        source="integration",
        message="amoCRM custom fields and pipeline statuses loaded.",
        details={"fields": len(fields), "statuses": len(statuses)},
    )
    session.commit()
    return render_integrations_page(
        request, session, owner_id,
        integration_notice=f"Загружено полей: {len(fields)}, статусов: {len(statuses)}.",
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
            "Тестовое уведомление доставлено. Интеграция Mattermost работает.",
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


def amocrm_configuration(integration: ProofIntegration) -> AmoIntegrationConfiguration:
    return AmoIntegrationConfiguration.model_validate(
        json_load(integration.configuration_json, {})
    )


def amocrm_status_payload(
    configuration: AmoIntegrationConfiguration,
    status_id: int,
    clear_field_ids: list[int] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"status_id": status_id}
    matching_status = next(
        (item for item in configuration.statuses_cache if item.get("id") == status_id),
        None,
    )
    if matching_status and isinstance(matching_status.get("pipeline_id"), int):
        payload["pipeline_id"] = matching_status["pipeline_id"]
    if clear_field_ids:
        payload["custom_fields_values"] = [
            {"field_id": field_id, "values": None}
            for field_id in clear_field_ids
        ]
    return payload


def update_amocrm_job_status(
    session: Session,
    integration: ProofIntegration,
    job: ProofJob,
    configuration: AmoIntegrationConfiguration,
    status_id: int | None,
    *,
    event_type: str,
    message: str,
    clear_field_ids: list[int] | None = None,
) -> bool:
    if status_id is None or job.crm_entity_type != "leads":
        return True
    try:
        with AmoClient(
            configuration.api_base_url,
            get_amocrm_access_token(session, settings, integration),
            timeout_seconds=configuration.api_timeout_seconds,
        ) as client:
            client.update_lead(
                job.crm_entity_id,
                amocrm_status_payload(configuration, status_id, clear_field_ids),
            )
    except (ValueError, ValidationError, RuntimeError, httpx.HTTPError) as exc:
        safe_error = sanitized_message(str(exc))
        integration.last_error_at = utc_now()
        integration.last_error_message = safe_error[:2000]
        add_event(
            session,
            owner_external_user_id=job.owner_external_user_id,
            job_id=job.id,
            integration_id=integration.id,
            event_type=f"{event_type}.failed",
            source="integration",
            level="error",
            message=f"{message} Не удалось обновить amoCRM.",
            error_code="AMOCRM_STATUS_UPDATE_FAILED",
            details={"status_id": status_id, "error": safe_error},
        )
        return False
    add_event(
        session,
        owner_external_user_id=job.owner_external_user_id,
        job_id=job.id,
        integration_id=integration.id,
        event_type=event_type,
        source="integration",
        message=message,
        details={"status_id": status_id, "cleared_field_ids": clear_field_ids or []},
    )
    return True


def update_amocrm_after_delivery(
    session: Session,
    job: ProofJob,
    *,
    delivered: bool,
) -> None:
    integration = session.scalar(select(ProofIntegration).where(
        ProofIntegration.owner_external_user_id == job.owner_external_user_id,
        ProofIntegration.kind == "amocrm",
    ))
    if integration is None:
        return
    try:
        configuration = amocrm_configuration(integration)
    except ValidationError:
        return
    if delivered and configuration.delivery_mode in {"amocrm_attachment", "yandex_disk_note"}:
        return
    update_amocrm_job_status(
        session,
        integration,
        job,
        configuration,
        configuration.completed_status_id if delivered else configuration.failed_status_id,
        event_type=("amocrm.lead.completed" if delivered else "amocrm.lead.delivery_failed"),
        message=(
            "Lead moved to the configured completed status."
            if delivered
            else "Lead moved to the configured failed status after delivery failure."
        ),
    )


@app.post("/webhooks/amocrm/{webhook_secret}")
async def amocrm_webhook(webhook_secret: str, request: Request, session: Session = Depends(get_db)) -> JSONResponse:
    request_id = str(uuid4())
    integration = next((row for row in session.scalars(select(ProofIntegration).where(
        ProofIntegration.kind == "amocrm", ProofIntegration.enabled.is_(True)
    )) if constant_time_matches(webhook_secret, row.webhook_secret_digest)), None)
    if integration is None:
        webhook_logger.warning(
            "amoCRM webhook rejected request_id=%s reason=invalid_credential client=%s",
            request_id,
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=401, detail="Invalid webhook credential.")
    content_type = request.headers.get("content-type", "")
    content_length = request.headers.get("content-length", "")
    client_address = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if not client_address and request.client:
        client_address = request.client.host
    incoming_details = {
        "request_id": request_id,
        "content_type": content_type[:200],
        "content_length": content_length[:40],
        "client_address": client_address[:120],
        "user_agent": request.headers.get("user-agent", "")[:300],
    }
    add_event(
        session,
        owner_external_user_id=integration.owner_external_user_id,
        integration_id=integration.id,
        event_type="webhook.incoming",
        source="integration",
        message=f"amoCRM webhook reached Proof Core. Request ID: {request_id}.",
        details=incoming_details,
        queue_notification=False,
    )
    integration.last_incoming_at = utc_now()
    session.commit()
    webhook_logger.info(
        "amoCRM webhook received request_id=%s integration_id=%s content_type=%s client=%s",
        request_id,
        integration.id,
        content_type[:200],
        client_address[:120],
    )
    is_json_webhook = "application/json" in content_type
    try:
        if is_json_webhook:
            raw_payload = await request.json()
        else:
            form = await request.form()
            raw_payload = {key: value for key, value in form.multi_items()}
        payload = (
            WebhookRequest.model_validate(raw_payload)
            if is_json_webhook
            else parse_amocrm_form_payload(raw_payload)
        )
        if not is_json_webhook:
            # amoCRM does not provide a delivery identifier. An identical payload can
            # represent another requested proof for the same lead, so every HTTP
            # delivery is an independent event. Explicit JSON integrations retain
            # their caller-supplied idempotency key.
            payload = payload.model_copy(update={"event_id": request_id})
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
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
            message=f"amoCRM webhook contract validation failed. Request ID: {request_id}.",
            error_code="WEBHOOK_VALIDATION_FAILED",
            details={**incoming_details, "error": sanitized_message(str(exc))[:1000]},
        )
        session.commit()
        detail = exc.errors() if isinstance(exc, ValidationError) else str(exc)
        raise HTTPException(status_code=422, detail=detail) from exc
    preset_id = payload.preset_id or integration.default_preset_id
    preset = session.get(ProofPreset, preset_id) if preset_id else None
    if preset is None or preset.owner_external_user_id != integration.owner_external_user_id:
        raise HTTPException(status_code=422, detail="Webhook has no valid Proof Preset.")
    try:
        configuration = amocrm_configuration(integration)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="amoCRM integration configuration is invalid.") from exc

    existing_receipt = session.scalar(select(WebhookReceipt).where(
        WebhookReceipt.integration_id == integration.id,
        WebhookReceipt.idempotency_key == payload.event_id,
    ))
    if existing_receipt is not None and is_json_webhook:
        job = session.get(ProofJob, existing_receipt.job_id) if existing_receipt.job_id else None
        if not is_json_webhook and job is not None and job.processing_status == "received":
            accepted_in_amo = update_amocrm_job_status(
                session,
                integration,
                job,
                configuration,
                configuration.queued_status_id,
                event_type="amocrm.lead.accepted",
                message="Lead fields cleared and lead moved to the configured processing status.",
                clear_field_ids=configuration.clear_field_ids,
            )
            if accepted_in_amo:
                queue_received_job(session, job)
                session.info["proof_worker_wake_pending"] = {
                    "owner_id": integration.owner_external_user_id,
                    "job_id": job.id,
                }
            session.commit()
            if not accepted_in_amo:
                raise HTTPException(status_code=502, detail="Не удалось подтвердить приём сделки в amoCRM.")
        add_event(
            session,
            owner_external_user_id=integration.owner_external_user_id,
            integration_id=integration.id,
            job_id=job.id if job else None,
            event_type="webhook.duplicate",
            source="integration",
            message=f"Duplicate amoCRM webhook handled. Request ID: {request_id}.",
            details={**incoming_details, "event_id": payload.event_id},
            queue_notification=False,
        )
        session.commit()
        return JSONResponse(
            {
                "accepted": existing_receipt.accepted,
                "duplicate": True,
                "job_id": job.id if job else None,
            },
            status_code=200,
        )

    input_payload = payload.input
    crm_order_id = payload.crm_order_id or payload.crm_entity_id
    if not is_json_webhook:
        if not configuration.has_required_mappings() or not configuration.clear_field_ids:
            raise HTTPException(
                status_code=422,
                detail="Для webhook amoCRM настройте три обязательных поля и поля для очистки.",
            )
        try:
            with AmoClient(
                configuration.api_base_url,
                get_amocrm_access_token(session, settings, integration),
                timeout_seconds=configuration.api_timeout_seconds,
            ) as client:
                lead = client.get_lead(payload.crm_entity_id)
            input_payload = build_job_input(lead, configuration)
        except (ValueError, ValidationError, RuntimeError, httpx.HTTPError) as exc:
            safe_error = sanitized_message(str(exc))
            designer_name = ""
            designer_field_id = configuration.mapping_for("designer_name")
            if designer_field_id is not None and "lead" in locals():
                designer_value = custom_field_value(lead, designer_field_id)
                designer_name = str(designer_value or "").strip()
            integration.last_incoming_at = utc_now()
            integration.last_error_at = utc_now()
            integration.last_error_message = safe_error[:2000]
            error_event = add_event(
                session,
                owner_external_user_id=integration.owner_external_user_id,
                integration_id=integration.id,
                event_type="amocrm.lead.read_failed",
                source="integration",
                level="error",
                message=f"amoCRM lead could not be read for Job creation: {safe_error}",
                error_code="AMOCRM_LEAD_READ_FAILED",
                details={
                    "request_id": request_id,
                    "crm_entity_id": payload.crm_entity_id,
                    "designer_name": designer_name,
                    "error": safe_error,
                },
            )
            # This webhook error must notify operators before the request ends.
            # Remove the generic after-commit trigger to avoid racing two dispatchers.
            session.info.pop("proof_notification_pending", None)
            session.commit()
            dispatch_pending_mattermost(
                session_factory,
                settings,
                event_id=error_event.id,
            )
            raise HTTPException(status_code=502, detail="Не удалось получить данные сделки amoCRM.") from exc

    if not crm_order_id:
        raise HTTPException(status_code=422, detail="Webhook amoCRM не содержит номер заказа.")

    job, duplicate, accepted = create_job_from_webhook(
        session,
        integration,
        idempotency_key=payload.event_id,
        event_type=payload.event_type,
        crm_entity_type=payload.crm_entity_type,
        crm_entity_id=payload.crm_entity_id,
        crm_order_id=crm_order_id,
        preset=preset,
        input_payload=input_payload,
        original_payload=raw_payload,
        enqueue=is_json_webhook,
    )
    session.commit()
    queued_after_amo_ack = False
    if accepted and job is not None and not is_json_webhook and (
        not duplicate or job.processing_status == "received"
    ):
        accepted_in_amo = update_amocrm_job_status(
            session,
            integration,
            job,
            configuration,
            configuration.queued_status_id,
            event_type="amocrm.lead.accepted",
            message="Lead fields cleared and lead moved to the configured processing status.",
            clear_field_ids=configuration.clear_field_ids,
        )
        if not accepted_in_amo:
            session.commit()
            raise HTTPException(status_code=502, detail="Не удалось подтвердить приём сделки в amoCRM.")
        queue_received_job(session, job)
        queued_after_amo_ack = True
    if accepted and job is not None and (not duplicate or queued_after_amo_ack):
        session.info["proof_worker_wake_pending"] = {
            "owner_id": integration.owner_external_user_id,
            "job_id": job.id,
        }
    session.commit()
    add_event(
        session,
        owner_external_user_id=integration.owner_external_user_id,
        integration_id=integration.id,
        job_id=job.id if job else None,
        event_type="webhook.processed",
        source="integration",
        message=(
            f"amoCRM webhook processed ({'accepted' if accepted else 'ignored'}). "
            f"Request ID: {request_id}."
        ),
        details={
            **incoming_details,
            "event_id": payload.event_id,
            "event_type": payload.event_type,
            "crm_entity_type": payload.crm_entity_type,
            "crm_entity_id": payload.crm_entity_id,
            "accepted": accepted,
            "duplicate": duplicate,
        },
        queue_notification=False,
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
        if job.delivery_status != "delivered":
            if job.delivery_status == "failed":
                delivered = retry_delivery(session, settings, job)
            else:
                delivered = deliver_result(session, settings, job)
            update_amocrm_after_delivery(session, job, delivered=delivered)
            session.commit()
        return job_payload(job)
    if not job.current_result_id:
        raise HTTPException(status_code=409, detail="Upload a Result before completing the Job.")
    transition_processing(session, job, "completed", source="worker", worker_id=worker.id,
                          message=f"Worker {worker.name} completed processing.")
    job.progress = 100
    job.completed_at = utc_now()
    release_worker(worker)
    delivered = deliver_result(session, settings, job)
    update_amocrm_after_delivery(session, job, delivered=delivered)
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
    integration = session.scalar(select(ProofIntegration).where(
        ProofIntegration.owner_external_user_id == job.owner_external_user_id,
        ProofIntegration.kind == "amocrm",
    ))
    if integration is not None:
        try:
            configuration = amocrm_configuration(integration)
        except ValidationError:
            configuration = None
        if configuration is not None:
            update_amocrm_job_status(
                session,
                integration,
                job,
                configuration,
                configuration.failed_status_id,
                event_type="amocrm.lead.processing_failed",
                message="Lead moved to the configured failed status after processing failure.",
            )
    session.commit()
    return job_payload(job)
