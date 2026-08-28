import asyncio
import os
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import unquote

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .db import Base, SessionLocal, engine, get_db
from .models import Cabinet, ReplyLog, ReplyTemplate, ScheduleSetting, User
from .scheduler import start_scheduler
from .security import hash_password, session_secret, verify_password
from .worker import (
    POSITIVE_LOW_RATING_MARKERS,
    PRODUCT_CATEGORIES,
    get_or_create_settings,
    preview_user_queue,
    process_user,
)

app = FastAPI(title="BellenneEcho")
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    session_cookie="bellenneecho_state",
    same_site="lax",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")
scheduler = None
MODULE_PREFIX = os.getenv("MODULE_PREFIX", "").rstrip("/")
EXTERNAL_AUTH_ENABLED = os.getenv("EXTERNAL_AUTH_ENABLED", "false").casefold() in {
    "1",
    "true",
    "yes",
    "on",
}


@app.on_event("startup")
def startup() -> None:
    global scheduler
    Base.metadata.create_all(bind=engine)
    apply_schema_updates()
    scheduler = start_scheduler()


@app.on_event("shutdown")
def shutdown() -> None:
    if scheduler:
        scheduler.shutdown(wait=False)


def apply_schema_updates() -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        user_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(users)"))}
        if "external_user_id" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN external_user_id INTEGER"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ix_users_external_user_id ON users(external_user_id) "
                "WHERE external_user_id IS NOT NULL"
            )
        )
        queue_table = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='processing_queue'")
        ).fetchone()
        if not queue_table:
            return

        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(processing_queue)"))}
        if "cabinet_name" not in columns:
            connection.execute(text("ALTER TABLE processing_queue ADD COLUMN cabinet_name VARCHAR(120) DEFAULT ''"))
        if "rating_raw" not in columns:
            connection.execute(text("ALTER TABLE processing_queue ADD COLUMN rating_raw VARCHAR(80) DEFAULT ''"))
        if "rating_source" not in columns:
            connection.execute(text("ALTER TABLE processing_queue ADD COLUMN rating_source VARCHAR(80) DEFAULT ''"))
        if "template_id" not in columns:
            connection.execute(text("ALTER TABLE processing_queue ADD COLUMN template_id INTEGER"))


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "BellenneEcho"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    return redirect("/dashboard" if current_user(request, db) else "/login")


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if current_user(request, db):
        return redirect("/dashboard")
    return render(request, "register.html", {"error": ""})


@app.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip()
    if len(username) < 3 or len(password) < 6:
        return render(request, "register.html", {"error": "Логин от 3 символов, пароль от 6 символов"})
    if db.scalar(select(User).where(User.username == username)):
        return render(request, "register.html", {"error": "Такой логин уже занят"})

    user = User(username=username, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    get_or_create_settings(db, user.id)
    request.session["user_id"] = user.id
    return redirect("/dashboard")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if current_user(request, db):
        return redirect("/dashboard")
    return render(request, "login.html", {"error": ""})


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(User.username == username.strip()))
    if not user or not verify_password(password, user.password_hash):
        return render(request, "login.html", {"error": "Проверьте логин и пароль"})
    request.session["user_id"] = user.id
    return redirect("/dashboard")


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return redirect("/login")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    settings = get_or_create_settings(db, user.id)
    stats = {
        "cabinets": db.scalar(select(func.count(Cabinet.id)).where(Cabinet.user_id == user.id)) or 0,
        "templates": db.scalar(select(func.count(ReplyTemplate.id)).where(ReplyTemplate.user_id == user.id)) or 0,
        "sent": db.scalar(select(func.count(ReplyLog.id)).where(ReplyLog.user_id == user.id, ReplyLog.status == "sent")) or 0,
        "errors": db.scalar(select(func.count(ReplyLog.id)).where(ReplyLog.user_id == user.id, ReplyLog.status == "error")) or 0,
    }
    latest_logs = db.scalars(
        select(ReplyLog).where(ReplyLog.user_id == user.id).order_by(ReplyLog.created_at.desc()).limit(6)
    ).all()
    return render(
        request,
        "dashboard.html",
        {"user": user, "stats": stats, "settings": settings, "next_run": next_run_text(settings), "logs": latest_logs},
    )


@app.get("/cabinets", response_class=HTMLResponse)
def cabinets_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    cabinets = db.scalars(select(Cabinet).where(Cabinet.user_id == user.id).order_by(Cabinet.created_at.desc())).all()
    return render(request, "cabinets.html", {"user": user, "cabinets": cabinets})


@app.post("/cabinets")
def create_cabinet(
    request: Request,
    name: str = Form(...),
    marketplace: str = Form(...),
    wb_api_key: str = Form(""),
    ozon_client_id: str = Form(""),
    ozon_api_key: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    cabinet = Cabinet(
        user_id=user.id,
        name=name.strip(),
        marketplace="wb" if marketplace == "wb" else "ozon",
        wb_api_key=wb_api_key.strip(),
        ozon_client_id=ozon_client_id.strip(),
        ozon_api_key=ozon_api_key.strip(),
    )
    db.add(cabinet)
    db.commit()
    return redirect("/cabinets")


@app.post("/cabinets/{cabinet_id}/toggle")
def toggle_cabinet(cabinet_id: int, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    cabinet = db.scalar(select(Cabinet).where(Cabinet.id == cabinet_id, Cabinet.user_id == user.id))
    if cabinet:
        cabinet.is_active = not cabinet.is_active
        db.add(cabinet)
        db.commit()
    return redirect("/cabinets")


@app.post("/cabinets/{cabinet_id}/delete")
def delete_cabinet(cabinet_id: int, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    cabinet = db.scalar(select(Cabinet).where(Cabinet.id == cabinet_id, Cabinet.user_id == user.id))
    if cabinet:
        db.delete(cabinet)
        db.commit()
    return redirect("/cabinets")


@app.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    items = db.scalars(
        select(ReplyTemplate).where(ReplyTemplate.user_id == user.id).order_by(ReplyTemplate.created_at.desc())
    ).all()
    return render(request, "templates.html", {"user": user, "items": items})


@app.post("/templates")
def create_template(
    request: Request,
    name: str = Form(...),
    match_mode: str = Form("all"),
    match_value: str = Form(""),
    review_kind: str = Form("any"),
    rating_from: int = Form(1),
    rating_to: int = Form(5),
    body: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    item = ReplyTemplate(
        user_id=user.id,
        name=name.strip(),
        match_mode=match_mode if match_mode in {"all", "category", "article"} else "all",
        match_value=match_value.strip(),
        review_kind=review_kind if review_kind in {"any", "text", "photo", "video"} else "any",
        rating_from=max(1, min(5, rating_from)),
        rating_to=max(1, min(5, rating_to)),
        body=body.strip(),
    )
    if item.match_mode == "all":
        item.match_value = ""
    elif item.match_mode == "category" and item.match_value not in PRODUCT_CATEGORIES:
        item.match_value = PRODUCT_CATEGORIES[0]
    if item.rating_from > item.rating_to:
        item.rating_from, item.rating_to = item.rating_to, item.rating_from
    db.add(item)
    db.commit()
    log_unsafe_template_range(db, user.id, item)
    return redirect("/templates")


@app.post("/templates/{template_id}/toggle")
def toggle_template(template_id: int, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    item = db.scalar(select(ReplyTemplate).where(ReplyTemplate.id == template_id, ReplyTemplate.user_id == user.id))
    if item:
        item.is_active = not item.is_active
        db.add(item)
        db.commit()
    return redirect("/templates")


@app.post("/templates/{template_id}/delete")
def delete_template(template_id: int, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    item = db.scalar(select(ReplyTemplate).where(ReplyTemplate.id == template_id, ReplyTemplate.user_id == user.id))
    if item:
        db.delete(item)
        db.commit()
    return redirect("/templates")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    settings = get_or_create_settings(db, user.id)
    return render(request, "settings.html", {"user": user, "settings": settings, "saved": request.query_params.get("saved")})


@app.post("/settings")
def save_settings(
    request: Request,
    interval_minutes: int = Form(60),
    max_replies_per_run: int = Form(20),
    quiet_start: str = Form(""),
    quiet_end: str = Form(""),
    enabled: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    settings = get_or_create_settings(db, user.id)
    settings.enabled = enabled == "on"
    settings.interval_minutes = max(5, min(1440, interval_minutes))
    settings.max_replies_per_run = max(1, min(100, max_replies_per_run))
    settings.quiet_start = quiet_start if quiet_start else ""
    settings.quiet_end = quiet_end if quiet_end else ""
    settings.updated_at = datetime.utcnow()
    db.add(settings)
    db.commit()
    return redirect("/settings?saved=1")


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    logs = db.scalars(
        select(ReplyLog).where(ReplyLog.user_id == user.id).order_by(ReplyLog.created_at.desc()).limit(120)
    ).all()
    return render(
        request,
        "logs.html",
        {
            "user": user,
            "logs": logs,
            "ran": request.query_params.get("ran"),
            "started": request.query_params.get("started"),
        },
    )


@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    items = asyncio.run(preview_user_queue(db, user))
    return render(request, "queue.html", {"user": user, "items": items})


@app.post("/run-now")
def run_now(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = require_user(request, db)
    if not user:
        return redirect("/login")
    background_tasks.add_task(run_user_now_background, user.id)
    return redirect("/logs?started=1")


def run_user_now_background(user_id: int) -> None:
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user:
            asyncio.run(process_user(db, user, manual=True))
    finally:
        db.close()


def render(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
    context = {
        **context,
        "request": request,
        "path": request.url.path,
        "marketplace_name": marketplace_name,
        "kind_name": kind_name,
        "status_name": status_name,
        "template_safety_warning": template_safety_warning,
        "mask": mask_secret,
        "product_categories": PRODUCT_CATEGORIES,
        "module_prefix": MODULE_PREFIX,
        "platform_csrf_token": getattr(request.state, "platform_csrf_token", ""),
    }
    return templates.TemplateResponse(
        request=request,
        name=template,
        context=context,
    )


def redirect(path: str) -> RedirectResponse:
    if EXTERNAL_AUTH_ENABLED and path in {"/login", "/register"}:
        return RedirectResponse(path, status_code=303)
    return RedirectResponse(f"{MODULE_PREFIX}{path}", status_code=303)


def current_user(request: Request, db: Session) -> User | None:
    if EXTERNAL_AUTH_ENABLED:
        raw_user_id = request.headers.get("X-Bellenne-User-Id", "")
        try:
            external_user_id = int(raw_user_id)
        except ValueError:
            return None
        if external_user_id <= 0:
            return None
        request.state.platform_csrf_token = unquote(
            request.headers.get("X-Bellenne-Csrf-Token", "")
        )
        username = unquote(request.headers.get("X-Bellenne-Username", "")) or f"bellenne-{external_user_id}"
        user = db.scalar(select(User).where(User.external_user_id == external_user_id))
        if user is None:
            user = db.scalar(select(User).where(User.username == username))
            if user is None:
                legacy_users = db.scalars(
                    select(User)
                    .where(User.external_user_id.is_(None))
                    .order_by(User.id)
                    .limit(2)
                ).all()
                if len(legacy_users) == 1:
                    user = legacy_users[0]
                    user.username = username
                    user.external_user_id = external_user_id
                else:
                    user = User(
                        username=username,
                        password_hash="external-auth",
                        external_user_id=external_user_id,
                    )
                    db.add(user)
                    db.flush()
                    get_or_create_settings(db, user.id)
            else:
                user.external_user_id = external_user_id
            db.commit()
            db.refresh(user)
        return user
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def require_user(request: Request, db: Session) -> User | None:
    return current_user(request, db)


def log_unsafe_template_range(db: Session, user_id: int, template: ReplyTemplate) -> None:
    warning = template_safety_warning(template)
    if not warning:
        return
    log = ReplyLog(
        user_id=user_id,
        cabinet_id=None,
        marketplace="system",
        review_id="",
        status="info",
        message=f"Шаблон '{template.name}' {warning}",
    )
    db.add(log)
    db.commit()


def template_safety_warning(template: ReplyTemplate) -> str:
    warnings: list[str] = []
    if template.rating_from < 4 <= template.rating_to:
        warnings.append(
            f"охватывает оценки {template.rating_from}–{template.rating_to}. "
            "Для оценок ниже 4 он будет заблокирован как слишком широкий."
        )
    if template.rating_from < 4 and _looks_positive_for_low_rating(template.body):
        warnings.append(
            "применяется к оценкам ниже 4, но текст похож на позитивный ответ. "
            "Проверьте формулировку, чтобы плохой отзыв не получил благодарность за высокую оценку."
        )
    return " ".join(warnings)


def _looks_positive_for_low_rating(text: str) -> bool:
    normalized = text.lower().replace("ё", "е")
    return any(marker in normalized for marker in POSITIVE_LOW_RATING_MARKERS)


def marketplace_name(value: str) -> str:
    return {"wb": "Wildberries", "ozon": "Ozon", "system": "Система"}.get(value, value)


def kind_name(value: str) -> str:
    return {"any": "Любой", "text": "Текст", "photo": "С фото", "video": "С видео"}.get(value, value)


def status_name(value: str) -> str:
    return {
        "queued": "В очереди",
        "processing": "В работе",
        "ready": "К обработке",
        "info": "Инфо",
        "sent": "Отправлен",
        "error": "Ошибка",
        "skipped": "Пропущен",
    }.get(value, value)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}••••{value[-4:]}"


def next_run_text(settings: ScheduleSetting) -> str:
    if not settings.enabled:
        return "Остановлено"
    if not settings.last_run_at:
        return "При ближайшей проверке"
    next_run = settings.last_run_at + timedelta(minutes=settings.interval_minutes)
    return next_run.strftime("%d.%m.%Y %H:%M")
