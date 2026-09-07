from __future__ import annotations

import hmac
import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from time import perf_counter
from typing import Any
from urllib.parse import unquote
from uuid import uuid4

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .config import AppSettings
from .core import Settings, process_images
from .database import build_engine, build_session_factory, init_database
from .models import ApiToken, RequestLog, utc_now
from .schemas import CalculateRequest, SettingsIn
from .security import masked_token
from .services import (
    authenticate_api_token,
    default_settings,
    find_api_token_for_user,
    load_configuration,
    rotate_api_token,
    save_configuration,
    summarize_calculation_result,
)


APP_VERSION = "1.1.0"
settings = AppSettings.from_env()
engine = build_engine(settings)
session_factory = build_session_factory(engine)
templates = Jinja2Templates(directory="app/templates")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database(engine)
    yield
    engine.dispose()


app = FastAPI(
    title="BellenneNest",
    version=APP_VERSION,
    description="Bellenne layout optimization engine.",
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


def parse_user_id(raw_value: str | None) -> int | None:
    try:
        value = int(raw_value or "")
    except ValueError:
        return None
    return value if value > 0 else None


def request_identity(request: Request) -> tuple[int | None, str]:
    external_user_id = parse_user_id(request.headers.get("X-Bellenne-User-Id"))
    username = unquote(request.headers.get("X-Bellenne-Username", "")).strip()
    if not username and external_user_id is not None:
        username = f"Пользователь #{external_user_id}"
    return external_user_id, username or "API-клиент"


def mes_requester(request: Request, *, required: bool = True) -> str:
    username = unquote(
        request.headers.get("X-MES-User")
        or request.headers.get("X-Bellenne-Username", "")
    ).strip()
    if not username and required:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-MES-User header is required.",
        )
    return username[:160]


def require_ui_identity(request: Request) -> tuple[int, str]:
    external_user_id, username = request_identity(request)
    if external_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bellenne identity header is required",
        )
    return external_user_id, username


def verify_platform_csrf(request: Request, submitted_token: str) -> None:
    expected = unquote(request.headers.get("X-Bellenne-Csrf-Token", ""))
    if not expected or not submitted_token or not hmac.compare_digest(expected, submitted_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


def require_api_token(
    auth_token: str | None = Header(default=None, alias="AUTH-TOKEN"),
    session: Session = Depends(get_db),
) -> ApiToken:
    credential = authenticate_api_token(session, auth_token or "")
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing AUTH-TOKEN header.",
        )
    return credential


def shared_context(request: Request, *, active: str, title: str) -> dict[str, Any]:
    _, username = request_identity(request)
    return {
        "request": request,
        "active": active,
        "title": title,
        "username": username,
        "module_prefix": settings.module_prefix,
        "platform_csrf_token": unquote(
            request.headers.get("X-Bellenne-Csrf-Token", "")
        ),
        "version": APP_VERSION,
    }


def render(
    request: Request,
    template_name: str,
    *,
    active: str,
    title: str,
    **context: Any,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={**shared_context(request, active=active, title=title), **context},
    )


def log_request(
    session: Session,
    *,
    request_id: str,
    external_user_id: int | None,
    username: str,
    endpoint: str,
    status_code: int,
    duration_ms: float,
    request_body: Any,
    response_body: Any,
    error_message: str | None = None,
) -> None:
    images = request_body.get("images") or request_body.get("items") or [] if isinstance(request_body, dict) else []
    is_ideal = response_body.get("is_ideal") if isinstance(response_body, dict) else None
    session.add(
        RequestLog(
            id=request_id,
            external_user_id=external_user_id,
            username=username[:160],
            endpoint=endpoint[:120],
            status_code=status_code,
            duration_ms=round(duration_ms, 2),
            item_count=len(images) if isinstance(images, list) else 0,
            is_ideal=is_ideal if isinstance(is_ideal, bool) else None,
            request_json=json.dumps(request_body, ensure_ascii=False, default=str),
            response_json=json.dumps(response_body, ensure_ascii=False, default=str),
            error_message=(error_message or "")[:2000] or None,
        )
    )
    session.commit()


def visible_logs_query(external_user_id: int, username: str):
    return select(RequestLog).where(
        or_(
            RequestLog.external_user_id == external_user_id,
            (
                (RequestLog.external_user_id.is_(None))
                & (RequestLog.username == username)
            ),
        )
    )


def usage_summaries(logs: list[RequestLog]) -> dict[str, dict[str, int | float]]:
    summaries: dict[str, dict[str, int | float]] = {}
    for log in logs:
        if log.status_code != 200:
            continue
        try:
            response_body = json.loads(log.response_json)
        except (TypeError, ValueError):
            continue
        if isinstance(response_body, dict):
            summaries[log.id] = summarize_calculation_result(response_body)
    return summaries


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    response_body = {"detail": exc.errors()}
    if request.url.path in {"/calculate", "/api/v1/calculate"}:
        with session_factory() as session:
            credential = authenticate_api_token(
                session,
                request.headers.get("AUTH-TOKEN", ""),
            )
            if credential is None:
                return JSONResponse(
                    {"detail": "Invalid or missing AUTH-TOKEN header."},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )
            username = mes_requester(request, required=False)
            if not username:
                return JSONResponse(
                    {"detail": "X-MES-User header is required."},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            credential.last_used_at = utc_now()
            log_request(
                session,
                request_id=str(uuid4()),
                external_user_id=credential.external_user_id,
                username=username,
                endpoint=request.url.path,
                status_code=422,
                duration_ms=0,
                request_body=exc.body,
                response_body=response_body,
                error_message="Запрос не прошёл проверку входных данных.",
            )
    return JSONResponse(jsonable_encoder(response_body), status_code=422)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "BellenneNest"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    external_user_id, username = require_ui_identity(request)
    api_token = find_api_token_for_user(session, external_user_id)
    base_query = visible_logs_query(external_user_id, username)
    recent_logs = list(
        session.scalars(base_query.order_by(RequestLog.created_at.desc()).limit(6))
    )
    total_requests = session.scalar(
        select(func.count()).select_from(base_query.subquery())
    ) or 0
    successful_requests = session.scalar(
        select(func.count()).select_from(
            base_query.where(RequestLog.status_code == 200).subquery()
        )
    ) or 0
    last_log = recent_logs[0] if recent_logs else None
    return render(
        request,
        "overview.html",
        active="overview",
        title="Обзор",
        recent_logs=recent_logs,
        total_requests=total_requests,
        successful_requests=successful_requests,
        last_log=last_log,
        api_configured=api_token is not None,
        usage=usage_summaries(recent_logs),
    )


@app.get("/configuration", response_class=HTMLResponse)
def configuration_page(
    request: Request,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    external_user_id, username = require_ui_identity(request)
    current = load_configuration(session, external_user_id, username)
    return render(
        request,
        "configuration.html",
        active="configuration",
        title="Конфигурация",
        configuration=current,
        saved=request.query_params.get("saved") == "1",
        error="",
    )


@app.post("/configuration", response_class=HTMLResponse)
def save_configuration_page(
    request: Request,
    csrf_token: str = Form(...),
    roll_length_m: float = Form(...),
    target_min_m: float = Form(...),
    upper_tolerance_m: float = Form(...),
    lower_soft_margin_m: float = Form(...),
    job_gap_cm: float = Form(...),
    leader_cm: float = Form(...),
    trailer_cm: float = Form(...),
    panel_gap_cm: float = Form(...),
    top_bottom_margin_cm: float = Form(...),
    default_height_cm: int = Form(...),
    brute_force_limit: int = Form(...),
    partial_max_r: int = Form(...),
    length_mode: str = Form(...),
    completion_objective: str = Form(...),
    completion_exact_groups: int = Form(...),
    completion_allowed_add_widths: str = Form(...),
    use_qty: str | None = Form(None),
    include_leader_trailer_in_target: str | None = Form(None),
    session: Session = Depends(get_db),
):
    external_user_id, username = require_ui_identity(request)
    verify_platform_csrf(request, csrf_token)
    try:
        allowed_widths = tuple(
            int(value.strip())
            for value in completion_allowed_add_widths.split(",")
            if value.strip()
        )
        configuration = SettingsIn(
            roll_length_m=roll_length_m,
            target_min_m=target_min_m,
            upper_tolerance_m=upper_tolerance_m,
            lower_soft_margin_m=lower_soft_margin_m,
            job_gap_cm=job_gap_cm,
            leader_cm=leader_cm,
            trailer_cm=trailer_cm,
            panel_gap_cm=panel_gap_cm,
            top_bottom_margin_cm=top_bottom_margin_cm,
            default_height_cm=default_height_cm,
            use_qty=use_qty == "on",
            brute_force_limit=brute_force_limit,
            partial_max_r=partial_max_r,
            length_mode=length_mode,
            include_leader_trailer_in_target=include_leader_trailer_in_target == "on",
            completion_objective=completion_objective,
            completion_exact_groups=completion_exact_groups,
            completion_allowed_add_widths=allowed_widths,
        )
    except (ValueError, ValidationError) as exc:
        session.rollback()
        return render(
            request,
            "configuration.html",
            active="configuration",
            title="Конфигурация",
            configuration=default_settings(),
            saved=False,
            error=str(exc),
        )
    save_configuration(session, external_user_id, username, configuration)
    session.commit()
    return RedirectResponse(
        f"{settings.module_prefix}/configuration?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/api-info", response_class=HTMLResponse)
def api_info(
    request: Request,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    external_user_id, _ = require_ui_identity(request)
    api_token = find_api_token_for_user(session, external_user_id)
    return render(
        request,
        "api.html",
        active="api",
        title="API",
        api_configured=api_token is not None,
        api_token=api_token,
        token_mask=(
            masked_token(api_token.token_prefix, api_token.token_last_four)
            if api_token is not None
            else "Не создан"
        ),
        generated_token="",
        token_replaced=False,
    )


@app.post("/api-token/generate", response_class=HTMLResponse)
def generate_api_token_page(
    request: Request,
    csrf_token: str = Form(...),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    external_user_id, username = require_ui_identity(request)
    verify_platform_csrf(request, csrf_token)
    token_replaced = find_api_token_for_user(session, external_user_id) is not None
    api_token, raw_token = rotate_api_token(
        session,
        external_user_id,
        username,
    )
    session.commit()
    response = render(
        request,
        "api.html",
        active="api",
        title="API",
        api_configured=True,
        api_token=api_token,
        token_mask=masked_token(api_token.token_prefix, api_token.token_last_four),
        generated_token=raw_token,
        token_replaced=token_replaced,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    external_user_id, username = require_ui_identity(request)
    logs = list(
        session.scalars(
            visible_logs_query(external_user_id, username)
            .order_by(RequestLog.created_at.desc())
            .limit(120)
        )
    )
    return render(
        request,
        "history.html",
        active="history",
        title="История запросов",
        logs=logs,
        usage=usage_summaries(logs),
    )


@app.get("/history/{request_id}", response_class=HTMLResponse)
def history_detail(
    request_id: str,
    request: Request,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    external_user_id, username = require_ui_identity(request)
    log = session.scalar(
        visible_logs_query(external_user_id, username).where(RequestLog.id == request_id)
    )
    if log is None:
        raise HTTPException(status_code=404, detail="Запрос не найден")
    request_body = json.loads(log.request_json)
    response_body = json.loads(log.response_json)
    return render(
        request,
        "history_detail.html",
        active="history",
        title=f"Запрос {log.id[:8]}",
        log=log,
        request_body=request_body,
        response_body=response_body,
        request_pretty=json.dumps(request_body, ensure_ascii=False, indent=2),
        response_pretty=json.dumps(response_body, ensure_ascii=False, indent=2),
        calculation_usage=(
            summarize_calculation_result(response_body)
            if log.status_code == 200 and isinstance(response_body, dict)
            else None
        ),
    )


@app.get("/settings/defaults")
def settings_defaults(
    credential: ApiToken = Depends(require_api_token),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    credential.last_used_at = utc_now()
    session.commit()
    return asdict(Settings())


@app.post("/calculate")
@app.post("/api/v1/calculate")
def calculate(
    payload: CalculateRequest,
    request: Request,
    session: Session = Depends(get_db),
    credential: ApiToken = Depends(require_api_token),
) -> JSONResponse:
    request_id = str(uuid4())
    started = perf_counter()
    external_user_id = credential.external_user_id
    username = mes_requester(request)
    credential.last_used_at = utc_now()
    request_body = payload.model_dump(mode="json", exclude_none=True)
    effective_settings = (
        payload.settings
        if "settings" in payload.model_fields_set
        else load_configuration(session, external_user_id, credential.owner_username)
    )
    images = [image.model_dump(exclude_none=True) for image in payload.images]
    try:
        result = process_images(images, Settings(**effective_settings.model_dump()))
    except ValueError as exc:
        response_body = {"detail": str(exc), "request_id": request_id}
        log_request(
            session,
            request_id=request_id,
            external_user_id=external_user_id,
            username=username,
            endpoint=request.url.path,
            status_code=422,
            duration_ms=(perf_counter() - started) * 1000,
            request_body=request_body,
            response_body=response_body,
            error_message=str(exc),
        )
        return JSONResponse(response_body, status_code=422, headers={"X-Nest-Request-Id": request_id})
    except Exception as exc:
        response_body = {
            "detail": "Calculation failed.",
            "request_id": request_id,
        }
        log_request(
            session,
            request_id=request_id,
            external_user_id=external_user_id,
            username=username,
            endpoint=request.url.path,
            status_code=500,
            duration_ms=(perf_counter() - started) * 1000,
            request_body=request_body,
            response_body=response_body,
            error_message=str(exc),
        )
        return JSONResponse(response_body, status_code=500, headers={"X-Nest-Request-Id": request_id})

    log_request(
        session,
        request_id=request_id,
        external_user_id=external_user_id,
        username=username,
        endpoint=request.url.path,
        status_code=200,
        duration_ms=(perf_counter() - started) * 1000,
        request_body=request_body,
        response_body=result,
    )
    return JSONResponse(result, headers={"X-Nest-Request-Id": request_id})
