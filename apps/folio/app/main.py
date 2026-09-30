import json
import hmac
import math
import os
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, image_tasks, mattermost, media, policy, presentation, retailcrm, scenarios, security

BASE = Path(__file__).parent
PREFIX = os.environ.get("MODULE_PREFIX", "/folio").rstrip("/")
# Docker uses copies of existing shared assets; local execution reads originals.
PULSE_CANDIDATE = BASE.parents[1] / "pulse" / "app"
PULSE = PULSE_CANDIDATE if PULSE_CANDIDATE.exists() else BASE
SHARED = BASE / "shared" if (BASE / "shared").exists() else PULSE / "static"
templates = Jinja2Templates(
    directory=[str(BASE / "templates"), str(PULSE / "templates")]
)
templates.env.filters["json"] = json.loads
templates.env.filters["folio_time"] = presentation.format_time
templates.env.globals.update(
    error_details=presentation.error_details,
    operation_error_details=presentation.operation_error_details,
    actor_label=presentation.actor_label,
    job_labels=presentation.JOB_LABELS,
    state_labels=presentation.STATE_LABELS,
    role_labels=presentation.ROLE_LABELS,
    actor_labels=presentation.ACTOR_LABELS,
    instance_labels=presentation.INSTANCE_LABELS,
    field_labels=presentation.FIELD_LABELS,
    object_labels=presentation.OBJECT_LABELS,
    audit_labels=presentation.AUDIT_LABELS,
)


@asynccontextmanager
async def lifespan(app):
    db.initialize()
    security.cipher()
    yield


app = FastAPI(
    title="BellenneFolio",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.mount("/shared", StaticFiles(directory=SHARED), name="shared")


@app.middleware("http")
async def private_response(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
    )
    return response


@app.exception_handler(scenarios.Invalid)
async def invalid(request, exc):
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={"prefix": PREFIX, "message": str(exc)},
        status_code=422,
    )


@app.exception_handler(sqlite3.IntegrityError)
async def conflict(request, exc):
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={
            "prefix": PREFIX,
            "message": "Запись уже существует или связанный объект недоступен. Обновите страницу.",
        },
        status_code=409,
    )


@app.exception_handler(HTTPException)
async def http_problem(request, exc):
    if "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={
            "prefix": PREFIX,
            "message": str(exc.detail) if exc.detail else "Действие недоступно",
        },
        status_code=exc.status_code,
    )


FORM_LABELS = {
    "name": "Название",
    "client_id": "Client ID",
    "api_key": "API-ключ",
    "text": "Текст ответа",
    "dedup": "Идентификатор формы",
    "node": "Шаг сценария",
    "item_id": "Товарная позиция",
    "chat_id": "Чат",
    "scenario_id": "Сценарий",
    "account_id": "Кабинет Ozon",
    "sku": "SKU",
    "media_id": "Изображение",
    "id": "Идентификатор",
    "role": "Роль",
    "revision": "Версия черновика",
    "version_id": "Версия сценария",
    "sync_minutes": "Период синхронизации",
    "handoff_hours": "Время до передачи",
    "base_url": "Адрес RetailCRM",
    "site": "Код магазина RetailCRM",
    "prompt": "Инструкция обработки изображения",
    "width_cm": "Ширина макета, см",
    "height_cm": "Высота макета, см",
}


def required(form, key):
    value = str(form.get(key, "")).strip()
    if not value:
        raise scenarios.Invalid(f"Заполните поле «{FORM_LABELS.get(key, 'Обязательное поле')}»")
    return value


def number(form, key, low=0, integer=False, optional=False):
    value = str(form.get(key, "")).strip()
    if not value and optional:
        return None
    try:
        result = int(value) if integer else float(value)
        if not math.isfinite(result) or result < low:
            raise ValueError()
        return result
    except ValueError:
        raise scenarios.Invalid(
            f"Проверьте числовое значение поля «{FORM_LABELS.get(key, 'Числовое поле')}»"
        ) from None


def redirect(path="/"):
    return RedirectResponse(PREFIX + path, status_code=303)


def render(request, con, actor, name, **context):
    context.update(
        {
            "prefix": PREFIX,
            "actor": actor,
            "csrf": unquote(request.headers.get("X-Bellenne-Csrf-Token", "")),
            "types": scenarios.TYPES,
            "kinds": scenarios.KINDS,
            "settings": db.config(con),
            "message_max_chars": policy.MESSAGE_MAX_CHARS,
        }
    )
    return templates.TemplateResponse(request=request, name=name, context=context)


def get_record(con, table, key):
    # Callers supply literal table names, never request values.
    value = con.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()
    if not value:
        raise HTTPException(404, "Объект не найден")
    return value


@app.get("/healthz")
def health():
    return {"status": "ok", "service": "BellenneFolio"}


@app.get("/auth/check")
def local_auth_check(request: Request):
    with db.transaction() as con:
        result = security.session_user(request.cookies.get(security.COOKIE_NAME, ""), con)
        if not result:
            raise HTTPException(401, "Войдите в Folio")
        actor, csrf_value = result
        return Response(status_code=204, headers={
            "X-Bellenne-User-Id": actor["id"],
            "X-Bellenne-Csrf-Token": csrf_value,
        })


@app.get("/login", response_class=HTMLResponse)
def local_login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html",
                                      context={"prefix": PREFIX, "error": ""})


@app.post("/login", response_class=HTMLResponse)
async def local_login(request: Request):
    origin = request.headers.get("origin")
    if origin:
        from urllib.parse import urlsplit
        parsed = urlsplit(origin)
        if parsed.netloc != request.headers.get("host") or parsed.scheme not in {"http", "https"}:
            raise HTTPException(403, "Недопустимый источник формы")
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    with db.transaction() as con:
        record = con.execute("SELECT * FROM users WHERE local_login=? AND role='manager'",
                             (username,)).fetchone()
        if not record or not record["password_hash"] or not security.check_password(password, record["password_hash"]) or con.execute(
            "SELECT 1 FROM revoked_users WHERE user_id=?", (record["id"],)
        ).fetchone():
            return templates.TemplateResponse(request=request, name="login.html",
                                              context={"prefix": PREFIX, "error": "Неверный логин или пароль"},
                                              status_code=401)
        token = security.issue_session(record, con)
        db.audit(con, record["id"], "user.login", "user", record["id"])
    response = redirect("/")
    response.set_cookie(security.COOKIE_NAME, token, max_age=security.SESSION_SECONDS,
                        path=PREFIX, httponly=True, samesite="lax",
                        secure=os.environ.get("FOLIO_SECURE_COOKIES", "").lower() == "true"
                        or request.headers.get("x-forwarded-proto") == "https")
    return response


@app.post("/logout")
async def local_logout(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con)
        security.revoke_session(request.cookies.get(security.COOKIE_NAME, ""), con)
        db.audit(con, actor["id"], "user.logout", "user", actor["id"])
    response = redirect("/login")
    response.delete_cookie(security.COOKIE_NAME, path=PREFIX)
    return response


@app.get("/", response_class=HTMLResponse)
@app.get("/chats/{chat_id}", response_class=HTMLResponse)
def inbox(
    request: Request,
    chat_id: int | None = None,
    q: str = "",
    mode: str = "",
    unread: bool = False,
):
    with db.transaction() as con:
        actor = security.user(request, con)
        pattern = f"%{q}%"
        clauses, params = (
            [
                "c.start_job_id IS NOT NULL",
                "c.chat_type='BUYER_SELLER'",
                "EXISTS(SELECT 1 FROM chat_items ci JOIN items i ON i.id=ci.item_id "
                "JOIN mappings m ON m.id=i.mapping_id WHERE ci.chat_id=c.id "
                "AND m.account_id=i.account_id AND m.key_kind='seller_article' "
                "AND m.sku=i.offer_id)",
                "(c.title LIKE ? OR c.external_id LIKE ? OR c.buyer_name LIKE ? "
                "OR c.buyer_name_manual LIKE ? OR c.buyer_id LIKE ? "
                "OR EXISTS(SELECT 1 FROM chat_items ci "
                "JOIN items i ON i.id=ci.item_id WHERE ci.chat_id=c.id AND "
                "(i.posting LIKE ? OR i.order_number LIKE ? OR i.customer_name LIKE ?)) "
                "OR EXISTS(SELECT 1 FROM messages sm WHERE sm.chat_id=c.id "
                "AND sm.order_number LIKE ?))"
            ],
            [pattern] * 9,
        )
        if actor["role"] != "admin" and db.config(con).get("manager_scope") != "all":
            clauses.append(
                "EXISTS(SELECT 1 FROM assignments a WHERE a.chat_id=c.id AND a.user_id=?)"
            )
            params.append(actor["id"])
        if mode in {"bot", "manual"}:
            clauses.append("c.mode=?")
            params.append(mode)
        if unread:
            clauses.append("c.unread>0")
        chats = con.execute(
            "SELECT c.*,"
            "COALESCE(NULLIF(c.buyer_name_manual,''),NULLIF(c.buyer_name,''),"
            "(SELECT NULLIF(i.customer_name,'') "
            "FROM items i JOIN chat_items ci ON ci.item_id=i.id WHERE ci.chat_id=c.id "
            "AND i.customer_name<>'' ORDER BY i.id LIMIT 1),"
            "CASE WHEN c.buyer_id<>'' THEN 'Покупатель ' || c.buyer_id END,"
            "'Диалог №' || c.id) AS display_name,"
            "COALESCE((SELECT NULLIF(m.order_number,'') FROM messages m WHERE m.chat_id=c.id "
            "AND m.order_number<>'' ORDER BY m.created_at DESC,m.id DESC LIMIT 1),"
            "NULLIF(active.order_number,''),"
            "(SELECT NULLIF(i.order_number,'') FROM items i JOIN chat_items ci ON ci.item_id=i.id "
            "WHERE ci.chat_id=c.id AND i.order_number<>'' ORDER BY i.id LIMIT 1),'') AS order_number,"
            "COALESCE(active.posting,"
            "(SELECT i.posting FROM items i JOIN chat_items ci ON ci.item_id=i.id "
            "WHERE ci.chat_id=c.id ORDER BY i.id LIMIT 1),'') AS posting_number,"
            "(SELECT CASE WHEN m.body='' AND m.media_ids<>'[]' THEN 'Фото' ELSE m.body END "
            "FROM messages m WHERE m.chat_id=c.id "
            "ORDER BY m.created_at DESC,m.id DESC LIMIT 1) AS last_message,"
            "active.external_status AS active_order_status "
            "FROM chats c LEFT JOIN items active ON active.id=c.active_item WHERE "
            + " AND ".join(clauses)
            + " ORDER BY c.updated_at DESC",
            params,
        ).fetchall()
        chat, messages, outgoing, items, instance, media_rows, nodes, history, integration_failures = (
            None,
            [],
            [],
            [],
            None,
            [],
            [],
            [],
            [],
        )
        if chat_id:
            chat = security.chat_access(con, actor, chat_id)
            chat = con.execute(
                "SELECT c.*,"
                "COALESCE(NULLIF(c.buyer_name_manual,''),NULLIF(c.buyer_name,''),"
                "(SELECT NULLIF(i.customer_name,'') "
                "FROM items i JOIN chat_items ci ON ci.item_id=i.id WHERE ci.chat_id=c.id "
                "AND i.customer_name<>'' ORDER BY i.id LIMIT 1),"
                "CASE WHEN c.buyer_id<>'' THEN 'Покупатель ' || c.buyer_id END,"
                "'Диалог №' || c.id) AS display_name,"
                "COALESCE((SELECT NULLIF(m.order_number,'') FROM messages m WHERE m.chat_id=c.id "
                "AND m.order_number<>'' ORDER BY m.created_at DESC,m.id DESC LIMIT 1),"
                "NULLIF(active.order_number,''),"
                "(SELECT NULLIF(i.order_number,'') FROM items i JOIN chat_items ci ON ci.item_id=i.id "
                "WHERE ci.chat_id=c.id AND i.order_number<>'' ORDER BY i.id LIMIT 1),'') AS order_number,"
                "COALESCE(active.posting,"
                "(SELECT i.posting FROM items i JOIN chat_items ci ON ci.item_id=i.id "
                "WHERE ci.chat_id=c.id ORDER BY i.id LIMIT 1),'') AS posting_number,"
                "active.external_status AS active_order_status "
                "FROM chats c LEFT JOIN items active ON active.id=c.active_item WHERE c.id=?",
                (chat_id,),
            ).fetchone()
            messages = con.execute(
                "SELECT m.*,v.number AS scenario_version FROM messages m "
                "LEFT JOIN outbox o ON o.chat_id=m.chat_id AND o.external_id=m.external_id "
                "LEFT JOIN instances i ON i.id=o.instance_id "
                "LEFT JOIN versions v ON v.id=i.version_id "
                "WHERE m.chat_id=? ORDER BY m.created_at,m.id",
                (chat_id,),
            ).fetchall()
            outgoing = con.execute(
                "SELECT * FROM outbox WHERE chat_id=? AND state NOT IN ('sent','cancelled') ORDER BY id",
                (chat_id,),
            ).fetchall()
            items = con.execute(
                "SELECT i.* FROM items i JOIN chat_items ci ON ci.item_id=i.id WHERE ci.chat_id=?",
                (chat_id,),
            ).fetchall()
            instance = con.execute(
                "SELECT i.*,v.number AS version_number FROM instances i JOIN versions v ON v.id=i.version_id WHERE i.chat_id=? AND i.item_id=?",
                (chat_id, chat["active_item"]),
            ).fetchone()
            media_rows = con.execute(
                "SELECT * FROM media WHERE chat_id=? ORDER BY created_at", (chat_id,)
            ).fetchall()
            if instance:
                nodes = json.loads(
                    con.execute(
                        "SELECT graph FROM versions WHERE id=?",
                        (instance["version_id"],),
                    ).fetchone()[0]
                )["nodes"]
                integration_failures = con.execute(
                    "SELECT error FROM retailcrm_actions WHERE instance_id=? AND state='failed' AND error IS NOT NULL "
                    "UNION ALL SELECT error FROM mattermost_actions WHERE instance_id=? AND state='failed' AND error IS NOT NULL",
                    (instance["id"], instance["id"]),
                ).fetchall()
            history = con.execute(
                "SELECT action,created_at FROM audit WHERE chat_id=? AND action NOT LIKE 'integration.%' ORDER BY id DESC LIMIT 50",
                (chat_id,),
            ).fetchall()
        return render(
            request,
            con,
            actor,
            "inbox.html",
            chats=chats,
            chat=chat,
            messages=messages,
            outgoing=outgoing,
            items=items,
            instance=instance,
            media=media_rows,
            nodes=nodes,
            history=history,
            integration_failures=integration_failures,
            q=q,
            mode=mode,
            unread=unread,
            dedup=uuid.uuid4().hex,
        )


@app.post("/chats/{chat_id}/{action}")
async def chat_action(request: Request, chat_id: int, action: str):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con)
        chat = security.chat_access(con, actor, chat_id)
        if action == "takeover":
            scenarios.takeover(con, chat_id, actor["id"])
        elif action == "read":
            con.execute("UPDATE chats SET unread=0 WHERE id=?", (chat_id,))
        elif action == "buyer_name":
            name = str(form.get("buyer_name") or "").strip()
            if len(name) > 100:
                raise scenarios.Invalid("Имя покупателя должно быть короче 100 символов")
            con.execute(
                "UPDATE chats SET buyer_name_manual=? WHERE id=?", (name, chat_id)
            )
            db.audit(con, actor["id"], "chat.buyer_name.updated", "chat", chat_id, chat_id)
        elif action == "send":
            text = required(form, "text")
            settings = db.config(con)
            if not settings.get("send_enabled") or len(text) > policy.MESSAGE_MAX_CHARS:
                raise scenarios.Invalid(
                    "Отправка отключена или текст длиннее 1000 символов"
                )
            key = required(form, "dedup")
            if not con.execute(
                "SELECT 1 FROM outbox WHERE dedup=?",
                (f"manual:{chat_id}:{actor['id']}:{key}",),
            ).fetchone():
                scenarios.takeover(con, chat_id, actor["id"])
                chat = get_record(con, "chats", chat_id)
                scenarios.queue_text(
                    con,
                    chat,
                    None,
                    text,
                    f"manual:{chat_id}:{actor['id']}:{key}",
                    f"manager:{actor['id']}",
                )
        elif action == "send_file":
            if not db.config(con).get("send_enabled"):
                raise scenarios.Invalid("Отправка Ozon не включена администратором")
            if not chat["active_item"]:
                raise scenarios.Invalid("Товарная позиция чата не определена")
            if con.execute("SELECT 1 FROM items WHERE id=? AND external_status='cancelled'",
                           (chat["active_item"],)).fetchone():
                raise scenarios.Invalid("Заказ отменён в Ozon. Макет отправлять нельзя")
            key = required(form, "dedup")
            dedup = f"manager-file:{chat_id}:{actor['id']}:{key}"
            if not con.execute("SELECT 1 FROM outbox WHERE dedup=?", (dedup,)).fetchone():
                instance = con.execute(
                    "SELECT i.* FROM instances i WHERE i.chat_id=? AND i.item_id=?",
                    (chat_id, chat["active_item"]),
                ).fetchone()
                waiting_mockup = False
                if instance and instance["status"] == "waiting_mockup" and chat["mode"] == "bot":
                    graph = json.loads(con.execute(
                        "SELECT graph FROM versions WHERE id=?", (instance["version_id"],)
                    ).fetchone()[0])
                    waiting_mockup = any(n["id"] == instance["node"] and n["kind"] == "await_mockup"
                                         for n in graph["nodes"])
                if waiting_mockup and con.execute(
                    "SELECT 1 FROM outbox WHERE instance_id=? AND kind='file' "
                    "AND state IN ('pending','unknown')", (instance["id"],)
                ).fetchone():
                    raise scenarios.Invalid("Предыдущее фото ещё ожидает подтверждения Ozon")
                upload = form.get("file")
                if not hasattr(upload, "read"):
                    raise scenarios.Invalid("Выберите изображение для отправки покупателю")
                content = await upload.read(policy.IMAGE_MAX_BYTES + 1)
                await upload.close()
                mid = media.store(con, content, db.config(con), chat_id, chat["active_item"])
                if not waiting_mockup:
                    scenarios.takeover(con, chat_id, actor["id"])
                    chat = get_record(con, "chats", chat_id)
                con.execute(
                    "INSERT INTO outbox(chat_id,instance_id,actor,body,dedup,epoch,created_at,kind,media_id) "
                    "VALUES(?,?,?,?,?,?,?,'file',?)",
                    (chat_id, instance["id"] if waiting_mockup else None,
                     f"manager:{actor['id']}", "Изображение макета", dedup,
                     chat["epoch"], db.now(), mid),
                )
                db.audit(con, actor["id"], "media.queued_to_buyer", "media", mid, chat_id)
        elif action == "resume":
            if actor["role"] != "admin" and not db.config(con).get("manager_resume"):
                raise HTTPException(403, "Возврат боту не разрешён политикой")
            scenarios.resume(con, chat_id, required(form, "node"), actor["id"])
        elif action == "approve":
            instance = con.execute(
                "SELECT id FROM instances WHERE chat_id=? AND item_id=?",
                (chat_id, chat["active_item"]),
            ).fetchone()
            if not instance:
                raise scenarios.Invalid("Нет активного брифа")
            scenarios.approve(con, instance[0], actor["id"])
        elif action == "attach":
            if not chat["active_item"]:
                raise scenarios.Invalid("Товарная позиция чата не определена")
            upload = form.get("file")
            if not hasattr(upload, "read"):
                raise scenarios.Invalid("Выберите изображение")
            content = await upload.read(policy.IMAGE_MAX_BYTES + 1)
            mid = media.store(
                con, content, db.config(con), chat_id, chat["active_item"]
            )
            # Explicit intake: manager may supply a buyer's photo, but it still
            # must pass the currently waiting photo node, never mark ready.
            con.execute(
                "INSERT INTO messages(chat_id,actor,body,media_ids,created_at) VALUES(?,?,?,?,?)",
                (
                    chat_id,
                    f"manager:{actor['id']}",
                    "Фото добавлено менеджером для брифа",
                    db.dump([mid]),
                    db.now(),
                ),
            )
            db.audit(con, actor["id"], "media.added", "media", mid, chat_id)
        elif action == "photo_answer":
            mid = required(form, "media_id")
            attachment = con.execute(
                "SELECT * FROM media WHERE id=? AND chat_id=?", (mid, chat_id)
            ).fetchone()
            if (
                not attachment
                or not chat["active_item"]
                or attachment["item_id"] not in (None, chat["active_item"])
            ):
                raise HTTPException(403, "Фото не принадлежит выбранному контексту")
            inst = con.execute(
                "SELECT * FROM instances WHERE chat_id=? AND item_id=?",
                (chat_id, chat["active_item"]),
            ).fetchone()
            if not inst:
                raise scenarios.Invalid("Нет активного сценария")
            if chat["mode"] != "bot":
                raise scenarios.Invalid("Явно верните бота на шаг фото перед приёмом")
            con.execute(
                "UPDATE media SET item_id=? WHERE id=?", (chat["active_item"], mid)
            )
            scenarios.advance(con, inst["id"], {"text": "", "media_ids": [mid]})
            db.audit(con, actor["id"], "media.accepted", "media", mid, chat_id)
        else:
            raise HTTPException(404)
    return redirect(f"/chats/{chat_id}")


@app.get("/media/{media_id}")
def private_media(request: Request, media_id: str):
    with db.transaction() as con:
        actor = security.user(request, con)
        row = get_record(con, "media", media_id)
        if row["chat_id"]:
            security.chat_access(con, actor, row["chat_id"])
        elif actor["role"] != "admin":
            # Only previews of templates allowed for the current assigned item.
            con.execute(
                "SELECT i.template_ids FROM instances i JOIN chats c ON c.id=i.chat_id WHERE i.item_id=c.active_item"
            ).fetchall()
            tid = con.execute(
                "SELECT id FROM templates WHERE media_id=? AND active=1", (media_id,)
            ).fetchone()
            accessible = False
            if tid:
                for inst in con.execute(
                    "SELECT * FROM instances WHERE item_id IN (SELECT active_item FROM chats)"
                ):
                    if str(tid[0]) in [
                        str(x) for x in json.loads(inst["template_ids"])
                    ]:
                        try:
                            security.chat_access(con, actor, inst["chat_id"])
                            accessible = True
                        except HTTPException:
                            pass
            if not accessible:
                raise HTTPException(403)
        path = db.DATA / "media" / row["id"]
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(
            path,
            media_type=row["mime"],
            headers={"Content-Disposition": 'inline; filename="image"'},
        )


@app.get("/settings")
@app.get("/settings/{section}")
def settings_page(request: Request, section: str = "overview"):
    if section not in {"overview", "ozon", "retailcrm", "mattermost", "image-worker", "automation", "users"}:
        raise HTTPException(404)
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        accounts = con.execute(
            "SELECT a.id,a.name,a.client_id,a.capabilities,a.checked_at,a.error,"
            "s.last_success FROM accounts a LEFT JOIN sync_state s ON s.account_id=a.id "
            "ORDER BY a.name"
        ).fetchall()
        capabilities = {
            account["id"]: json.loads(account["capabilities"]) for account in accounts
        }
        account_jobs = {}
        for job in con.execute(
            "SELECT id,kind,account_id,state,error,created_at,finished_at "
            "FROM jobs WHERE kind IN ('probe','sync') ORDER BY id DESC"
        ).fetchall():
            account_jobs.setdefault(job["account_id"], job)
        account_unknown_starts = {}
        for job in con.execute(
            "SELECT id,account_id,state,error,finished_at,"
            "json_extract(payload,'$.posting') AS posting "
            "FROM jobs WHERE kind='start_chat' AND state='unknown' "
            "AND NOT EXISTS(SELECT 1 FROM items i WHERE i.account_id=jobs.account_id "
            "AND i.posting=json_extract(jobs.payload,'$.posting') "
            "AND i.external_status='cancelled') ORDER BY id DESC"
        ).fetchall():
            account_unknown_starts.setdefault(job["account_id"], []).append(job)
        account_error_jobs = {}
        for job in con.execute(
            "SELECT id,kind,account_id,state,error,finished_at FROM jobs "
            "WHERE error IS NOT NULL ORDER BY id DESC"
        ).fetchall():
            account_error_jobs.setdefault(job["account_id"], job)
        current_settings = db.config(con)
        retailcrm_config = retailcrm.integration(con)
        mattermost_config = mattermost.integration(con)
        image_worker_record = image_tasks.integration(con)
        image_worker_config = (
            {key: image_worker_record[key] for key in (
                "base_url", "checked_at", "error", "revision"
            )}
            if image_worker_record else None
        )
        retailcrm_capabilities = (
            json.loads(retailcrm_config["capabilities"])
            if retailcrm_config
            else {}
        )
        retailcrm_job = con.execute(
            "SELECT id,state,error,created_at,finished_at FROM jobs "
            "WHERE kind='retailcrm_probe' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        try:
            retailcrm.public_media_base_url()
            retailcrm_media_url_error = None
        except retailcrm.RetailCRMError as exc:
            retailcrm_media_url_error = exc.code
        onboarding = {
            "connection": bool(accounts),
            "capabilities": any(
                all(caps.get(key) is True for key in ("account", "orders", "list"))
                for caps in capabilities.values()
            ),
            "mappings": bool(con.execute(
                "SELECT 1 FROM mappings WHERE key_kind='seller_article' AND active=1 LIMIT 1"
            ).fetchone()),
            "scenarios": bool(con.execute(
                "SELECT 1 FROM mappings m JOIN scenarios s ON s.id=m.scenario_id "
                "WHERE m.key_kind='seller_article' AND m.active=1 AND s.published IS NOT NULL LIMIT 1"
            ).fetchone()),
            "automation": bool(current_settings.get("automation_enabled")),
        }
        return render(
            request,
            con,
            actor,
            "settings.html",
            accounts=accounts,
            account_capabilities=capabilities,
            account_jobs=account_jobs,
            account_unknown_starts=account_unknown_starts,
            account_error_jobs=account_error_jobs,
            retailcrm_config=retailcrm_config,
            mattermost_config=mattermost_config,
            image_worker_config=image_worker_config,
            retailcrm_capabilities=retailcrm_capabilities,
            retailcrm_job=retailcrm_job,
            retailcrm_media_url_error=retailcrm_media_url_error,
            section=section,
            onboarding=onboarding,
            users=con.execute(
                "SELECT u.*,NOT EXISTS(SELECT 1 FROM revoked_users r WHERE r.user_id=u.id) AS enabled FROM users u"
            ).fetchall(),
        )


@app.post("/settings/ozon")
async def save_ozon_policy(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        settings = dict(db.config(con))
        settings["sync_minutes"] = number(
            form, "sync_minutes", 0.001, optional=True
        )


        settings["sync_enabled"] = form.get("sync_enabled") == "on"
        since = str(form.get("sync_since", "")).strip()
        if since:
            try:
                parsed = datetime.fromisoformat(since)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                settings["sync_since"] = parsed.isoformat()
            except ValueError:
                raise scenarios.Invalid("Неверная дата начала синхронизации") from None
        else:
            settings["sync_since"] = None
        if settings["sync_enabled"] and not all(
            settings.get(k)
            for k in ("sync_minutes", "sync_since")
        ):
            raise scenarios.Invalid(
                "Для периодической синхронизации укажите дату начала импорта и период"
            )
        con.execute("UPDATE settings SET value=? WHERE id=1", (db.dump(settings),))
        db.audit(con, actor["id"], "settings.ozon_updated", "settings", 1)
    return redirect("/settings/ozon")


@app.post("/settings/retailcrm")
async def save_retailcrm(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        try:
            base_url = retailcrm.normalize_base_url(required(form, "base_url"))
        except ValueError as exc:
            raise scenarios.Invalid(str(exc)) from exc
        site = required(form, "site")
        key = str(form.get("api_key", "")).strip()
        current = retailcrm.integration(con)
        if not current and not key:
            raise scenarios.Invalid("Введите API-ключ RetailCRM")
        changed = (
            not current
            or base_url != current["base_url"]
            or site != current["site"]
            or bool(key)
        )
        if changed and con.execute(
            "SELECT 1 FROM jobs WHERE kind IN ('retailcrm_create','retailcrm_note') AND state='running'"
        ).fetchone():
            raise scenarios.Invalid(
                "Дождитесь завершения текущего создания сделки перед сменой подключения RetailCRM"
            )
        secret = (
            security.cipher().encrypt(key.encode()).decode()
            if key
            else current["secret"]
        )
        if current:
            con.execute(
                "UPDATE retailcrm_integrations SET base_url=?,site=?,secret=?,"
                "capabilities=?,statuses=?,checked_at=?,error=?,revision=revision+1 WHERE id=1",
                (
                    base_url,
                    site,
                    secret,
                    "{}" if changed else current["capabilities"],
                    "[]" if changed else current["statuses"],
                    None if changed else current["checked_at"],
                    None if changed else current["error"],
                ),
            )
        else:
            con.execute(
                "INSERT INTO retailcrm_integrations(id,base_url,site,secret) "
                "VALUES(1,?,?,?)",
                (base_url, site, secret),
            )
        db.audit(con, actor["id"], "settings.retailcrm_updated", "settings", 1)
    return redirect("/settings/retailcrm")


@app.post("/settings/mattermost")
async def save_mattermost(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        current = mattermost.integration(con)
        raw = str(form.get("webhook_url") or "").strip()
        if not current and not raw:
            raise scenarios.Invalid("Укажите входящий webhook Mattermost")
        if raw:
            try:
                url = mattermost.normalize_webhook_url(raw)
            except ValueError as exc:
                raise scenarios.Invalid(str(exc)) from exc
            secret = security.cipher().encrypt(url.encode()).decode()
            if current:
                if con.execute("SELECT 1 FROM jobs WHERE kind='mattermost_send' AND state='running'").fetchone():
                    raise scenarios.Invalid("Дождитесь завершения отправки в Mattermost")
                con.execute("UPDATE mattermost_integrations SET secret=?,revision=revision+1 WHERE id=1", (secret,))
            else:
                con.execute("INSERT INTO mattermost_integrations(id,secret) VALUES(1,?)", (secret,))
            db.audit(con, actor["id"], "settings.mattermost_updated", "settings", 1)
    return redirect("/settings/mattermost")


@app.post("/settings/image-worker")
async def save_image_worker(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        current = image_tasks.integration(con)
        public_base = os.environ.get("FOLIO_PUBLIC_BASE_URL", "").strip().rstrip("/")
        if not public_base:
            raise scenarios.Invalid("Сначала задайте FOLIO_PUBLIC_BASE_URL — публичный HTTPS-адрес Folio")
        callback = f"{public_base}/folio/image-worker/webhook"
        allow_http = str(form.get("base_url") or "").strip().startswith("http://")
        try:
            base, callback, input_base = image_tasks.validate_urls(required(form, "base_url"), callback,
                                                                    public_base, allow_http)
        except image_tasks.ImageTaskError as exc:
            raise scenarios.Invalid("Проверьте адрес воркера и FOLIO_PUBLIC_BASE_URL") from exc
        api_key = str(form.get("api_key") or "").strip()
        if not current and not api_key:
            raise scenarios.Invalid("Введите ключ воркера")
        changed = (not current or base != current["base_url"] or callback != current["webhook_url"]
                   or input_base != current["input_base_url"] or allow_http != bool(current["allow_http"])
                   or bool(api_key))
        if changed and con.execute("SELECT 1 FROM image_jobs WHERE state='submitting' LIMIT 1").fetchone():
            raise scenarios.Invalid("Дождитесь завершения текущей передачи задания воркеру")
        cipher = security.cipher()
        if not api_key:
            api_key = cipher.decrypt(current["api_key_secret"].encode()).decode()
        key_encrypted = cipher.encrypt(api_key.encode()).decode()
        webhook_encrypted = cipher.encrypt(api_key.encode()).decode()
        vendor_encrypted = ""
        if current:
            con.execute(
                "UPDATE image_worker_integrations SET base_url=?,api_key_secret=?,webhook_secret=?,"
                "webhook_url=?,input_base_url=?,allow_http=?,vendor_token_secret=?,checked_at=?,error=?,"
                "revision=revision+1 WHERE id=1",
                (base, key_encrypted, webhook_encrypted, callback, input_base, int(allow_http),
                 vendor_encrypted, None if changed else current["checked_at"],
                 None if changed else current["error"]),
            )
        else:
            con.execute(
                "INSERT INTO image_worker_integrations(id,base_url,api_key_secret,webhook_secret,"
                "webhook_url,input_base_url,allow_http,vendor_token_secret) VALUES(1,?,?,?,?,?,?,?)",
                (base, key_encrypted, webhook_encrypted, callback, input_base, int(allow_http), vendor_encrypted),
            )
        db.audit(con, actor["id"], "settings.image_worker_updated", "settings", 1)
    return redirect("/settings/image-worker")


@app.post("/settings/image-worker/probe")
async def probe_image_worker(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        current = image_tasks.integration(con)
        if not current:
            raise scenarios.Invalid("Сначала сохраните подключение воркера изображений")
        revision = current["revision"]
    try:
        ok, error = image_tasks.probe()
    except image_tasks.ImageTaskError as exc:
        ok, error = False, exc.code
    with db.transaction() as con:
        if con.execute("SELECT revision FROM image_worker_integrations WHERE id=1").fetchone()[0] == revision:
            con.execute("UPDATE image_worker_integrations SET checked_at=?,error=? WHERE id=1",
                        (db.now(), None if ok else error))
            db.audit(con, actor["id"], "integration.image_worker_probe", "image_worker_integration", 1)
    return redirect("/settings/image-worker")


@app.post("/settings/retailcrm/probe")
async def probe_retailcrm(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        if not retailcrm.integration(con):
            raise scenarios.Invalid("Сначала сохраните подключение RetailCRM")
        db.job(con, "retailcrm_probe")
        db.audit(
            con,
            actor["id"],
            "integration.queued_retailcrm_probe",
            "retailcrm_integration",
            1,
        )
    return redirect("/settings/retailcrm")


@app.post("/settings/automation")
async def save_automation(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        settings = dict(db.config(con))
        was_automated = bool(settings.get("automation_enabled") and settings.get("start_enabled"))
        settings["handoff_hours"] = number(
            form, "handoff_hours", 0, optional=True
        )
        settings["send_enabled"] = form.get("send_enabled") == "on"
        settings["start_enabled"] = form.get("auto_start_enabled") == "on"
        settings["automation_enabled"] = settings["start_enabled"]
        if settings["automation_enabled"] and not was_automated:
            settings["automation_enabled_at"] = db.now()
        elif not settings["automation_enabled"]:
            settings.pop("automation_enabled_at", None)
        settings.pop("start_statuses", None)
        settings["manager_resume"] = form.get("manager_resume") == "on"
        for key, options in {
            "manager_scope": {"all", "assigned"},
            "timer_origin": {"confirmation", "review"},
        }.items():
            value = form.get(key)
            if value not in options:
                raise scenarios.Invalid("Выберите один из предложенных вариантов")
            settings[key] = value
        if settings["automation_enabled"]:
            if not settings["send_enabled"]:
                raise scenarios.Invalid(
                    "Перед автоматизацией включите отправку и подтвердите её в тестовом чате"
                )
            account_rows = con.execute(
                "SELECT capabilities FROM accounts WHERE checked_at IS NOT NULL AND error IS NULL"
            ).fetchall()
            required_capabilities = ["account", "orders", "list"]
            if not any(
                all(json.loads(row[0]).get(key) is True for key in required_capabilities)
                and all(json.loads(row[0]).get(key) is not False for key in (
                    "history", "send", "attachments"
                ))
                and (not settings["start_enabled"] or json.loads(row[0]).get("start") is not False)
                for row in account_rows
            ):
                raise scenarios.Invalid(
                    "Для автоматизации подтвердите кабинет, заказы и список чатов Ozon; "
                    "известные отказы в истории, отправке, вложениях или создании чата блокируют запуск"
                )
            if not con.execute(
                "SELECT 1 FROM mappings m JOIN scenarios s ON s.id=m.scenario_id "
                "WHERE m.key_kind='seller_article' AND m.active=1 AND s.published IS NOT NULL LIMIT 1"
            ).fetchone():
                raise scenarios.Invalid(
                    "Привяжите хотя бы один артикул продавца к опубликованному сценарию"
                )
        con.execute("UPDATE settings SET value=? WHERE id=1", (db.dump(settings),))
        db.audit(con, actor["id"], "settings.automation_updated", "settings", 1)
    return redirect("/settings/automation")


@app.post("/accounts")
async def save_account(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        name, client = required(form, "name"), required(form, "client_id")
        key, aid = (
            str(form.get("api_key", "")).strip(),
            number(form, "id", 1, True, True),
        )
        if aid:
            old = get_record(con, "accounts", aid)
            if con.execute(
                "SELECT 1 FROM jobs WHERE account_id=? AND state='running'", (aid,)
            ).fetchone():
                raise scenarios.Invalid(
                    "Дождитесь завершения текущей операции кабинета перед сменой ключа"
                )
            if not key and client != old["client_id"]:
                raise scenarios.Invalid(
                    "При смене Client-Id введите соответствующий ключ"
                )
            secret = (
                security.cipher().encrypt(key.encode()).decode()
                if key
                else old["secret"]
            )
            if key or client != old["client_id"]:
                con.execute(
                    "UPDATE accounts SET name=?,client_id=?,secret=?,capabilities='{}',checked_at=NULL,error=NULL,revision=revision+1 WHERE id=?",
                    (name, client, secret, aid),
                )
                con.execute("DELETE FROM ozon_products WHERE account_id=?", (aid,))
                db.job(con, "catalog_sync", aid)
            else:
                con.execute("UPDATE accounts SET name=? WHERE id=?", (name, aid))
        else:
            if not key:
                raise scenarios.Invalid("Введите API-ключ")
            aid = con.execute(
                "INSERT INTO accounts(name,client_id,secret) VALUES(?,?,?)",
                (name, client, security.cipher().encrypt(key.encode()).decode()),
            ).lastrowid
        db.audit(con, actor["id"], "account.saved", "account", aid)
    return redirect("/settings/ozon")


@app.post("/accounts/{account_id}/{action}")
async def account_action(request: Request, account_id: int, action: str):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        get_record(con, "accounts", account_id)
        if action not in {"probe", "sync"}:
            raise HTTPException(404)
        if action == "sync" and not db.config(con).get("sync_since"):
            raise scenarios.Invalid(
                "Укажите дату начала импорта на странице Ozon перед синхронизацией"
            )
        db.job(con, action, account_id)
        db.audit(
            con, actor["id"], f"integration.queued_{action}", "account", account_id
        )
    return redirect("/settings/ozon")


@app.post("/users")
async def save_user(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        uid, name, role = (
            required(form, "id"),
            required(form, "name"),
            required(form, "role"),
        )
        if role not in {"admin", "manager"}:
            raise scenarios.Invalid("Допустимы только роли «Администратор» и «Менеджер»")
        enabled = form.get("enabled") == "on"
        if uid == actor["id"] and (role != "admin" or not enabled):
            raise scenarios.Invalid("Нельзя снять собственные права администратора")
        con.execute(
            "INSERT INTO users(id,name,role) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,role=excluded.role",
            (uid, name, role),
        )
        if enabled:
            con.execute("DELETE FROM revoked_users WHERE user_id=?", (uid,))
        else:
            con.execute("INSERT OR IGNORE INTO revoked_users VALUES(?)", (uid,))
        db.audit(con, actor["id"], "user.updated", "user", uid)
    return redirect("/settings/users")


@app.post("/users/local")
async def save_local_manager(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    login = required(form, "username").lower()
    name = required(form, "name")
    password = str(form.get("password") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,39}", login):
        raise scenarios.Invalid("Логин: 3–40 латинских букв, цифр, точек, дефисов или подчёркиваний")
    if len(name) > 100 or len(password) < 12 or len(password) > 128:
        raise scenarios.Invalid("Укажите имя до 100 символов и пароль от 12 до 128 символов")
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        uid = f"folio:{login}"
        con.execute(
            "INSERT INTO users(id,name,role,local_login,password_hash) VALUES(?,?,'manager',?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,role='manager',"
            "local_login=excluded.local_login,password_hash=excluded.password_hash",
            (uid, name, login, security.password_hash(password)),
        )
        con.execute("DELETE FROM revoked_users WHERE user_id=?", (uid,))
        db.audit(con, actor["id"], "user.local_updated", "user", uid)
    return redirect("/settings/users")


@app.get("/scenarios")
@app.get("/scenarios/{scenario_id}")
def scenario_page(request: Request, scenario_id: int | None = None):
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        record = get_record(con, "scenarios", scenario_id) if scenario_id else None
        if record:
            graph = json.loads(record["draft"])
            if scenarios.normalize(graph, record["product_type"]):
                con.execute(
                    "UPDATE scenarios SET draft=?,revision=revision+1 WHERE id=?",
                    (db.dump(graph), scenario_id),
                )
                record = get_record(con, "scenarios", scenario_id)
        versions = con.execute(
            "SELECT id,number,author,created_at FROM versions WHERE scenario_id=? ORDER BY number DESC",
            (scenario_id,),
        ).fetchall()
        return render(
            request,
            con,
            actor,
            "scenarios.html",
            records=con.execute("SELECT * FROM scenarios").fetchall(),
            record=record,
            nodes=json.loads(record["draft"])["nodes"] if record else [],
            versions=versions,
            retailcrm_statuses=json.loads(retailcrm.integration(con)["statuses"]) if retailcrm.integration(con) else [],
        )


@app.post("/scenarios")
async def create_scenario(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        product_type = required(form, "product_type")
        if product_type not in scenarios.TYPES:
            raise scenarios.Invalid("Неизвестный тип товара")
        sid = con.execute(
            "INSERT INTO scenarios(name,product_type,draft) VALUES(?,?,?)",
            (
                required(form, "name"),
                product_type,
                db.dump(scenarios.initial_graph(product_type)),
            ),
        ).lastrowid
        db.audit(con, actor["id"], "scenario.created", "scenario", sid)
    return redirect(f"/scenarios/{sid}")


@app.get("/scenarios/{scenario_id}/export")
def export_scenario(request: Request, scenario_id: int, source: str = "draft"):
    if source not in {"draft", "published"}:
        raise HTTPException(400, "Выберите черновик или опубликованную версию")
    with db.transaction() as con:
        security.user(request, con, admin=True)
        record = get_record(con, "scenarios", scenario_id)
        if source == "published":
            if not record["published"]:
                raise HTTPException(404, "Опубликованной версии пока нет")
            version = con.execute("SELECT graph FROM versions WHERE id=? AND scenario_id=?",
                                  (record["published"], scenario_id)).fetchone()
            graph = json.loads(version["graph"])
        else:
            graph = json.loads(record["draft"])
        package = scenarios.export_package(record["name"], record["product_type"], graph)
        return Response(
            content=json.dumps(package, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="folio-scenario-{scenario_id}-{source}.json"'},
        )


@app.post("/scenarios/import")
async def import_scenario(request: Request):
    form = await request.form()
    upload = form.get("file")
    try:
        security.csrf(request, form.get("csrf"))
        with db.transaction() as con:
            actor = security.user(request, con, admin=True)
            if not upload or not hasattr(upload, "read"):
                raise scenarios.Invalid("Выберите JSON-файл сценария")
            raw = await upload.read(scenarios.TRANSFER_LIMIT + 1)
            name, product_type, graph = scenarios.import_package(raw)
            sid = con.execute(
                "INSERT INTO scenarios(name,product_type,draft) VALUES(?,?,?)",
                (name, product_type, db.dump(graph)),
            ).lastrowid
            db.audit(con, actor["id"], "scenario.imported", "scenario", sid)
    finally:
        if upload and hasattr(upload, "close"):
            await upload.close()
    return redirect(f"/scenarios/{sid}")


def graph_from_form(form):
    serialized = str(form.get("graph_json", "")).strip()
    if not serialized:
        raise scenarios.Invalid(
            "Не удалось получить данные визуальной схемы. Обновите страницу и повторите сохранение."
        )
    try:
        graph = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise scenarios.Invalid("Данные визуальной схемы повреждены") from exc
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        raise scenarios.Invalid("Визуальная схема должна содержать список шагов")
    return graph


def preview_history(form):
    serialized = str(form.get("history_json", "[]"))
    try:
        history = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise scenarios.Invalid("История тестового диалога повреждена") from exc
    if not isinstance(history, list) or len(history) > 100:
        raise scenarios.Invalid("Начните тестовый диалог заново")
    clean = []
    for event in history:
        if not isinstance(event, dict) or event.get("kind") not in {
            "text",
            "photo",
            "finish_photos",
            "manager_photo",
            "approval_timeout",
        }:
            raise scenarios.Invalid("История тестового диалога повреждена")
        if event["kind"] == "text":
            value = str(event.get("value", "")).strip()
            if not value or len(value) > policy.MESSAGE_MAX_CHARS:
                raise scenarios.Invalid("Тестовое сообщение должно содержать от 1 до 1000 символов")
            clean.append({"kind": "text", "value": value})
        elif event["kind"] == "photo":
            clean.append({"kind": "photo", "count": 1})
        elif event["kind"] in {"manager_photo", "approval_timeout"}:
            clean.append({"kind": event["kind"]})
        else:
            clean.append({"kind": "finish_photos"})
    return clean


def render_preview(request, con, actor, record, history):
    from .simulation import simulate

    graph = json.loads(record["draft"])
    scenarios.normalize(graph, record["product_type"])
    try:
        result = simulate(graph, record["product_type"], history)
        problem = None
    except scenarios.Invalid as exc:
        result = None
        problem = str(exc)
    return render(
        request,
        con,
        actor,
        "preview.html",
        result=result,
        record=record,
        history=history,
        problem=problem,
    )


@app.get("/scenarios/{scenario_id}/preview")
def scenario_preview(request: Request, scenario_id: int):
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        record = get_record(con, "scenarios", scenario_id)
        return render_preview(request, con, actor, record, [])


@app.post("/scenarios/{scenario_id}/{action}")
async def scenario_action(request: Request, scenario_id: int, action: str):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        record = get_record(con, "scenarios", scenario_id)
        if action == "copy":
            sid = con.execute(
                "INSERT INTO scenarios(name,product_type,draft) VALUES(?,?,?)",
                (record["name"] + " — копия", record["product_type"], record["draft"]),
            ).lastrowid
            return redirect(f"/scenarios/{sid}")
        if action == "rollback":
            version = con.execute(
                "SELECT graph FROM versions WHERE id=? AND scenario_id=?",
                (form.get("version_id"), scenario_id),
            ).fetchone()
            if not version:
                raise HTTPException(404)
            con.execute(
                "UPDATE scenarios SET draft=?,revision=revision+1 WHERE id=?",
                (version[0], scenario_id),
            )
            scenarios.publish(con, scenario_id, actor["id"])
        elif action == "publish":
            if form.get("confirm_publish") != "yes":
                raise scenarios.Invalid("Подтвердите публикацию")
            scenarios.publish(con, scenario_id, actor["id"])
        elif action in {"save", "validate"}:
            if number(form, "revision", 1, True) != record["revision"]:
                raise HTTPException(409, "Черновик изменён другим администратором")
            graph = graph_from_form(form)
            scenarios.normalize(graph, record["product_type"])
            if action == "validate":
                scenarios.validate(graph, record["product_type"])
            con.execute(
                "UPDATE scenarios SET draft=?,revision=revision+1 WHERE id=?",
                (db.dump(graph), scenario_id),
            )
        elif action == "preview":
            history = [] if form.get("preview_action") == "reset" else preview_history(form)
            action_value = str(form.get("preview_action", "start"))
            if len(history) >= 100 and action_value not in {"start", "reset"}:
                raise scenarios.Invalid(
                    "В тестовом диалоге слишком много шагов. Начните его заново"
                )
            if action_value == "reply":
                message = str(form.get("message", "")).strip()
                if not message or len(message) > policy.MESSAGE_MAX_CHARS:
                    raise scenarios.Invalid(
                        "Тестовое сообщение должно содержать от 1 до 1000 символов"
                    )
                history.append({"kind": "text", "value": message})
            elif action_value == "photo":
                history.append({"kind": "photo", "count": 1})
            elif action_value == "finish_photos":
                history.append({"kind": "finish_photos"})
            elif action_value in {"manager_photo", "approval_timeout"}:
                history.append({"kind": action_value})
            elif action_value not in {"start", "reset"}:
                raise scenarios.Invalid("Неизвестное действие тестового диалога")
            return render_preview(request, con, actor, record, history)
        else:
            raise HTTPException(404)
        db.audit(con, actor["id"], f"scenario.{action}", "scenario", scenario_id)
    return redirect(f"/scenarios/{scenario_id}")


@app.get("/catalog")
def catalog_page(request: Request):
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        accounts = con.execute("SELECT id,name FROM accounts").fetchall()
        requested_account = request.query_params.get("account_id", "")
        selected_account_id = (
            int(requested_account)
            if requested_account.isdigit()
            and any(row["id"] == int(requested_account) for row in accounts)
            else (accounts[0]["id"] if accounts else None)
        )
        for account in accounts:
            if not con.execute(
                "SELECT 1 FROM ozon_products WHERE account_id=? LIMIT 1", (account["id"],)
            ).fetchone() and not con.execute(
                "SELECT 1 FROM jobs WHERE account_id=? AND kind='catalog_sync' LIMIT 1",
                (account["id"],),
            ).fetchone():
                db.job(con, "catalog_sync", account["id"])
        return render(
            request,
            con,
            actor,
            "catalog.html",
            accounts=accounts,
            selected_account_id=selected_account_id,
            mappings=con.execute(
                "SELECT m.*,a.name AS account_name,s.name AS scenario_name "
                "FROM mappings m JOIN accounts a ON a.id=m.account_id "
                "JOIN scenarios s ON s.id=m.scenario_id "
                "WHERE m.key_kind='seller_article' AND m.active=1 ORDER BY a.name,m.sku"
            ).fetchall(),
            legacy_mappings=con.execute(
                "SELECT m.sku,a.name AS account_name FROM mappings m "
                "JOIN accounts a ON a.id=m.account_id "
                "WHERE m.key_kind='ozon_sku' AND m.active=1 ORDER BY a.name,m.sku"
            ).fetchall(),
            records=con.execute("SELECT * FROM scenarios").fetchall(),
            catalog=con.execute("SELECT * FROM templates").fetchall(),
        )


@app.get("/catalog/products")
def catalog_products(request: Request, account_id: int):
    with db.transaction() as con:
        security.user(request, con, admin=True)
        get_record(con, "accounts", account_id)
        latest = con.execute(
            "SELECT state,error,finished_at FROM jobs WHERE account_id=? "
            "AND kind='catalog_sync' ORDER BY id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        products = [
            row[0] for row in con.execute(
                "SELECT offer_id FROM ozon_products WHERE account_id=? ORDER BY offer_id",
                (account_id,),
            )
        ]
        return JSONResponse({
            "products": products,
            "state": latest["state"] if latest else "not_started",
            "error": latest["error"] if latest else None,
            "updated_at": latest["finished_at"] if latest and latest["state"] == "done" else None,
        })


@app.post("/catalog/products/refresh")
async def catalog_products_refresh(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        account_id = number(form, "account_id", 1, True)
        get_record(con, "accounts", account_id)
        db.job(con, "catalog_sync", account_id)
        db.audit(con, actor["id"], "catalog.refresh_queued", "account", account_id)
    return redirect(f"/catalog?account_id={account_id}")


@app.post("/mappings")
async def mapping_save(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        account_id = number(form, "account_id", 1, True)
        get_record(con, "accounts", account_id)
        scenario = get_record(con, "scenarios", number(form, "scenario_id", 1, True))
        if not scenario["published"]:
            raise scenarios.Invalid("Сначала опубликуйте сценарий")
        tids = form.getlist("template_ids")
        for tid in tids:
            if not get_record(con, "templates", tid)["active"]:
                raise scenarios.Invalid("Выбран неактивный шаблон")
        if scenario["product_type"] == "template_art" and not tids:
            raise scenarios.Invalid("Выберите разрешённые шаблоны")
        sku = required(form, "sku")
        if not con.execute(
            "SELECT 1 FROM ozon_products WHERE account_id=? AND offer_id=?",
            (account_id, sku),
        ).fetchone():
            raise scenarios.Invalid(
                "Выберите артикул продавца из загруженного списка Ozon"
            )
        con.execute(
            "INSERT INTO mappings(account_id,sku,product_type,scenario_id,template_ids,key_kind,active) VALUES(?,?,?,?,?,'seller_article',1) ON CONFLICT(account_id,sku) DO UPDATE SET product_type=excluded.product_type,scenario_id=excluded.scenario_id,template_ids=excluded.template_ids,key_kind='seller_article',active=1",
            (
                account_id,
                sku,
                scenario["product_type"],
                scenario["id"],
                db.dump(tids),
            ),
        )
        mid = con.execute(
            "SELECT id FROM mappings WHERE account_id=? AND sku=?",
            (account_id, sku),
        ).fetchone()[0]
        con.execute(
            "UPDATE items SET mapping_id=? WHERE account_id=? AND offer_id=? AND id NOT IN (SELECT item_id FROM instances)",
            (mid, account_id, sku),
        )
        db.audit(con, actor["id"], "mapping.updated", "mapping", mid)
    return redirect("/catalog")


def _mapping_has_running_start(con, mapping_id):
    return con.execute(
        "SELECT 1 FROM jobs j JOIN items i ON i.account_id=j.account_id "
        "AND i.posting=json_extract(j.payload,'$.posting') "
        "WHERE j.kind='start_chat' AND j.state='running' AND i.mapping_id=? LIMIT 1",
        (mapping_id,),
    ).fetchone() is not None


@app.post("/mappings/{mapping_id}/edit")
async def mapping_edit(request: Request, mapping_id: int):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        mapping = get_record(con, "mappings", mapping_id)
        if not mapping["active"] or mapping["key_kind"] != "seller_article":
            raise HTTPException(404)
        if _mapping_has_running_start(con, mapping_id):
            raise scenarios.Invalid("Для этого артикула сейчас создаётся чат. Дождитесь завершения и повторите изменение.")
        scenario = get_record(con, "scenarios", number(form, "scenario_id", 1, True))
        if not scenario["published"]:
            raise scenarios.Invalid("Сначала опубликуйте сценарий")
        tids = form.getlist("template_ids")
        for tid in tids:
            if not get_record(con, "templates", tid)["active"]:
                raise scenarios.Invalid("Выбран неактивный шаблон")
        if scenario["product_type"] == "template_art" and not tids:
            raise scenarios.Invalid("Выберите разрешённые шаблоны")
        con.execute(
            "UPDATE mappings SET scenario_id=?,product_type=?,template_ids=? WHERE id=?",
            (scenario["id"], scenario["product_type"], db.dump(tids), mapping_id),
        )
        db.audit(con, actor["id"], "mapping.updated", "mapping", mapping_id)
    return redirect("/catalog")


@app.post("/mappings/{mapping_id}/delete")
async def mapping_delete(request: Request, mapping_id: int):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        if form.get("confirm") != "1":
            raise scenarios.Invalid("Подтвердите удаление привязки")
        mapping = get_record(con, "mappings", mapping_id)
        if not mapping["active"] or mapping["key_kind"] != "seller_article":
            raise HTTPException(404)
        if _mapping_has_running_start(con, mapping_id):
            raise scenarios.Invalid("Для этого артикула сейчас создаётся чат. Дождитесь завершения и повторите удаление.")
        con.execute("UPDATE mappings SET active=0 WHERE id=?", (mapping_id,))
        con.execute(
            "UPDATE items SET mapping_id=NULL WHERE mapping_id=? "
            "AND id NOT IN (SELECT item_id FROM instances)",
            (mapping_id,),
        )
        db.audit(con, actor["id"], "mapping.deleted", "mapping", mapping_id)
    return redirect("/catalog")


@app.post("/templates")
async def template_save(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        tid = number(form, "id", 1, True, True)
        name = required(form, "name")
        mid = get_record(con, "templates", tid)["media_id"] if tid else None
        upload = form.get("file")
        if hasattr(upload, "read") and upload.filename:
            mid = media.store(
                con,
                await upload.read(policy.IMAGE_MAX_BYTES + 1),
                db.config(con),
            )
        if not mid:
            raise scenarios.Invalid("Добавьте превью шаблона")
        if tid:
            con.execute(
                "UPDATE templates SET name=?,media_id=?,active=? WHERE id=?",
                (name, mid, int(form.get("active") == "on"), tid),
            )
        else:
            tid = con.execute(
                "INSERT INTO templates(name,media_id,active) VALUES(?,?,?)",
                (name, mid, int(form.get("active") == "on")),
            ).lastrowid
        db.audit(con, actor["id"], "template.saved", "template", tid)
    return redirect("/catalog")


@app.get("/orders")
def orders_page(request: Request):
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        return render(
            request,
            con,
            actor,
            "orders.html",
            items=con.execute(
                "SELECT i.*,a.name AS account_name,a.capabilities AS account_capabilities,"
                "a.error AS account_error,s.status AS brief_status,s.id AS instance_id,"
                "(SELECT c.id FROM chats c JOIN chat_items ci ON ci.chat_id=c.id "
                "WHERE ci.item_id=i.id AND c.start_job_id IS NOT NULL LIMIT 1) AS folio_chat_id,"
                "(SELECT j.state FROM jobs j WHERE j.kind='start_chat' "
                "AND j.account_id=i.account_id AND json_extract(j.payload,'$.posting')=i.posting "
                "ORDER BY j.id DESC LIMIT 1) AS start_state,"
                "(SELECT j.error FROM jobs j WHERE j.kind='start_chat' "
                "AND j.account_id=i.account_id AND json_extract(j.payload,'$.posting')=i.posting "
                "ORDER BY j.id DESC LIMIT 1) AS start_error,"
                "(SELECT COUNT(*) FROM items sibling JOIN mappings sm ON sm.id=sibling.mapping_id "
                "JOIN scenarios ss ON ss.id=sm.scenario_id WHERE sibling.account_id=i.account_id "
                "AND sibling.posting=i.posting AND sm.key_kind='seller_article' AND sm.active=1 "
                "AND sm.sku=sibling.offer_id AND ss.published IS NOT NULL) AS managed_count "
                "FROM items i JOIN accounts a ON a.id=i.account_id "
                "JOIN mappings m ON m.id=i.mapping_id "
                "JOIN scenarios configured ON configured.id=m.scenario_id "
                "LEFT JOIN instances s ON s.item_id=i.id "
                "WHERE m.account_id=i.account_id AND m.key_kind='seller_article' "
                "AND (m.active=1 OR s.id IS NOT NULL) "
                "AND m.sku=i.offer_id AND configured.published IS NOT NULL "
                "ORDER BY i.id DESC"
            ).fetchall(),
        )


@app.get("/briefs/{instance_id}/export")
def export(request: Request, instance_id: int):
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        instance = get_record(con, "instances", instance_id)
        item = get_record(con, "items", instance["item_id"])
        graph = json.loads(get_record(con, "versions", instance["version_id"])["graph"])
        if (
            instance["status"] != "ready"
            or not scenarios.complete(
                con, instance, graph, json.loads(instance["fields"])
            )
            or not scenarios.settled(con, instance)
        ):
            raise scenarios.Invalid("Бриф ещё не готов к передаче")
        payload = {
            **dict(instance),
            "fields": json.loads(instance["fields"]),
            "item": dict(item),
            "delivery": "export_only",
        }
        product_type = con.execute(
            "SELECT s.product_type FROM scenarios s JOIN versions v ON v.scenario_id=s.id WHERE v.id=?",
            (instance["version_id"],),
        ).fetchone()[0]
        payload["product_type"] = product_type
        payload["photos"] = [
            dict(
                con.execute(
                    "SELECT id,mime,size,digest FROM media WHERE id=?", (mid,)
                ).fetchone()
            )
            for mid in payload["fields"].get("photos", [])
        ]
        db.audit(
            con,
            actor["id"],
            "brief.exported",
            "instance",
            instance_id,
            instance["chat_id"],
        )
        return JSONResponse(
            payload,
            headers={
                "Content-Disposition": f'attachment; filename="folio-brief-{instance_id}.json"'
            },
        )


@app.get("/image-jobs")
def image_jobs_page(request: Request):
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        return render(
            request, con, actor, "image_jobs.html",
            worker_configured=image_tasks.configured(con),
            image_job_labels=image_tasks.STATE_LABELS,
            image_job_errors=image_tasks.ERROR_LABELS,
            image_jobs=con.execute(
                "SELECT j.*,i.posting,i.name AS product_name,inst.chat_id "
                "FROM image_jobs j JOIN items i ON i.id=j.item_id "
                "LEFT JOIN instances inst ON inst.item_id=j.item_id "
                "ORDER BY j.id DESC LIMIT 200"
            ).fetchall(),
        )


@app.post("/image-jobs")
async def create_image_job(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        media_id = required(form, "media_id")
        media_row = con.execute("SELECT item_id FROM media WHERE id=?", (media_id,)).fetchone()
        if not media_row or not media_row["item_id"]:
            raise scenarios.Invalid("Выберите сохранённое фото заказа")
        try:
            image_tasks.enqueue(
                con, media_row["item_id"], media_id,
                required(form, "prompt"), number(form, "width_cm", 1),
                number(form, "height_cm", 1), actor["id"],
            )
        except image_tasks.ImageTaskError as exc:
            raise scenarios.Invalid({
                "image_worker_not_configured": "Сначала настройте подключение к воркеру изображений.",
                "image_worker_prompt_invalid": "Инструкция должна содержать от 1 до 4000 символов.",
                "image_worker_dimensions_invalid": "Проверьте размер макета: 1–300 см и не более 100 млн пикселей.",
                "image_worker_media_missing": "Выберите сохранённое фото именно этой товарной позиции.",
            }.get(exc.code, "Не удалось поставить обработку изображения в очередь.")) from exc
    return redirect("/image-jobs")


@app.post("/image-worker/webhook")
async def image_worker_webhook(request: Request):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 16_384:
            raise HTTPException(413, "Слишком большое уведомление")
        chunks.append(chunk)
    try:
        matched = image_tasks.accept_webhook(
            b"".join(chunks), request.headers.get("X-Folio-Signature", ""),
            request.headers.get("X-Folio-Event", ""),
            request.headers.get("X-Folio-Delivery-Id", ""),
        )
    except image_tasks.ImageTaskError:
        raise HTTPException(401, "Недействительное уведомление воркера") from None
    if not matched:
        # The worker may complete before its 202 response is committed here.
        # A retry is safer than acknowledging an orphaned result.
        raise HTTPException(503, "Задание ещё не зарегистрировано")
    return {"ok": True}


@app.get("/image-worker/input/{token}")
def image_worker_input(token: str):
    return image_worker_input_at(token, 0)


@app.get("/image-worker/input/{token}/{index}")
def image_worker_input_at(token: str, index: int):
    if len(token) < 32 or len(token) > 128:
        raise HTTPException(404, "Изображение недоступно")
    with db.transaction() as con:
        row = con.execute(
            "SELECT j.media_id,j.media_ids,j.item_id FROM image_jobs j "
            "WHERE j.input_token=? AND j.state NOT IN ('completed','failed')", (token,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Изображение недоступно")
        media_ids = json.loads(row["media_ids"]) or [row["media_id"]]
        if not 0 <= index < len(media_ids):
            raise HTTPException(404, "Изображение недоступно")
        media_id = media_ids[index]
        media = con.execute("SELECT mime FROM media WHERE id=? AND item_id=?", (media_id, row["item_id"])).fetchone()
        source = db.DATA / "media" / media_id
        if not media or not source.is_file():
            raise HTTPException(404, "Изображение недоступно")
        return FileResponse(source, media_type=media["mime"])


@app.post("/image-worker/vendor-callback/{token}")
async def image_worker_vendor_callback(request: Request, token: str):
    expected = image_tasks.vendor_token()
    if len(expected) < 32 or not hmac.compare_digest(token, expected):
        raise HTTPException(404, "Недоступно")
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 16_384:
            raise HTTPException(413, "Слишком большое уведомление")
    # Nano Banana requires a callback URL, while FolioImageWorker's polling
    # remains the source of truth. Never change job state from this callback.
    return Response(status_code=204)


@app.get("/image-jobs/{image_job_id}/preview")
def image_job_preview(request: Request, image_job_id: int):
    with db.transaction() as con:
        security.user(request, con, admin=True)
        record = get_record(con, "image_jobs", image_job_id)
        if record["state"] != "completed" or not record["worker_id"]:
            raise HTTPException(404, "Предпросмотр ещё не готов")
        worker_id = record["worker_id"]
    try:
        content = image_tasks.preview(worker_id)
    except image_tasks.ImageTaskError:
        raise HTTPException(503, "Предпросмотр сейчас недоступен") from None
    return Response(content, media_type="image/jpeg")


@app.get("/audit")
def audit_page(request: Request):
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        return render(
            request,
            con,
            actor,
            "audit.html",
            events=con.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT 300"
            ).fetchall(),
            jobs=con.execute(
                "SELECT j.id,j.kind,j.account_id,j.state,j.error,j.created_at,j.finished_at,a.name AS account_name "
                "FROM jobs j LEFT JOIN accounts a ON a.id=j.account_id ORDER BY j.id DESC LIMIT 100"
            ).fetchall(),
            outgoing=con.execute(
                "SELECT id,chat_id,state,error,external_id FROM outbox WHERE state IN ('unknown','failed')"
            ).fetchall(),
        )


@app.post("/outbox/{outbox_id}/resolve")
async def resolve_send(request: Request, outbox_id: int):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        record = get_record(con, "outbox", outbox_id)
        if record["state"] not in {"unknown", "failed"}:
            raise scenarios.Invalid("Сообщение уже обработано")
        external_id = str(form.get("external_id", "")).strip()
        if form.get("retry") == "yes":
            if record["state"] != "failed":
                raise scenarios.Invalid(
                    "Неопределённую отправку нельзя повторять без сверки истории"
                )
            con.execute(
                "UPDATE outbox SET state='pending',error=NULL,external_id=NULL WHERE id=?",
                (outbox_id,),
            )
            audit_action = "outbound.retry_queued"
        elif external_id:
            match = con.execute(
                "SELECT * FROM messages WHERE chat_id=? AND external_id=? AND actor='seller'",
                (record["chat_id"], external_id),
            ).fetchone()
            if match and record["kind"] == "file":
                expected = con.execute("SELECT digest FROM media WHERE id=? AND chat_id=?",
                                       (record["media_id"], record["chat_id"])).fetchone()
                match = expected and any(
                    con.execute("SELECT 1 FROM media WHERE id=? AND digest=?",
                                (mid, expected["digest"])).fetchone()
                    for mid in json.loads(match["media_ids"])
                )
            elif match:
                match = match["body"] == record["body"]
            if not match:
                raise scenarios.Invalid(
                    "В синхронизированной истории нет соответствующего сообщения продавца"
                )
            con.execute(
                "UPDATE outbox SET state='sent',external_id=?,error=NULL WHERE id=?",
                (external_id, outbox_id),
            )
            scenarios.outgoing_confirmed(con, outbox_id)
            audit_action = "outbound.reconciled"
        elif form.get("cancel") == "yes":
            con.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (outbox_id,))
            audit_action = "outbound.reconciled"
        else:
            raise scenarios.Invalid(
                "Выберите безопасный повтор, укажите подтверждённый ID сообщения или закройте попытку"
            )
        db.audit(
            con,
            actor["id"],
            audit_action,
            "outbox",
            outbox_id,
            record["chat_id"],
        )
    return redirect("/audit")


@app.post("/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: int):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        record = get_record(con, "jobs", job_id)
        if record["state"] != "failed":
            raise scenarios.Invalid(
                "Повтор разрешён только для заведомо неуспешной операции. Неопределённый результат требует ручной сверки."
            )
        if record["kind"] in {"retailcrm_create", "retailcrm_note", "mattermost_send"}:
            raise scenarios.Invalid(
                "Ошибка создания сделки уже обработана веткой сценария. Повторите действие из RetailCRM или запустите новый сценарий после исправления причины."
            )
        con.execute(
            "UPDATE jobs SET state='pending',error=NULL,finished_at=NULL WHERE id=?",
            (job_id,),
        )
        if record["kind"] == "start_chat":
            account = con.execute(
                "SELECT capabilities FROM accounts WHERE id=?", (record["account_id"],)
            ).fetchone()
            caps = json.loads(account[0])
            caps.pop("start", None)
            caps.pop("start_error", None)
            con.execute(
                "UPDATE accounts SET capabilities=?,error=NULL WHERE id=?",
                (db.dump(caps), record["account_id"]),
            )
            con.execute(
                "UPDATE sync_state SET blocked_until=NULL WHERE account_id=?",
                (record["account_id"],),
            )
        db.audit(con, actor["id"], "integration.retry_requested", "job", job_id)
    return redirect("/audit")
