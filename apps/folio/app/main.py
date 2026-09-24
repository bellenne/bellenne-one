import json
import math
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, media, policy, presentation, retailcrm, scenarios, security

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
        clauses, params = (
            ["(c.title LIKE ? OR c.external_id LIKE ?)"],
            [f"%{q}%", f"%{q}%"],
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
            "SELECT c.*,(SELECT m.body FROM messages m WHERE m.chat_id=c.id "
            "ORDER BY m.created_at DESC,m.id DESC LIMIT 1) AS last_message "
            "FROM chats c WHERE "
            + " AND ".join(clauses)
            + " ORDER BY c.updated_at DESC",
            params,
        ).fetchall()
        chat, messages, outgoing, items, instance, media_rows, nodes, history = (
            None,
            [],
            [],
            [],
            None,
            [],
            [],
            [],
        )
        if chat_id:
            chat = security.chat_access(con, actor, chat_id)
            messages = con.execute(
                "SELECT m.*,v.number AS scenario_version FROM messages m "
                "LEFT JOIN outbox o ON o.chat_id=m.chat_id AND o.external_id=m.external_id "
                "LEFT JOIN instances i ON i.id=o.instance_id "
                "LEFT JOIN versions v ON v.id=i.version_id "
                "WHERE m.chat_id=? ORDER BY m.created_at,m.id",
                (chat_id,),
            ).fetchall()
            outgoing = con.execute(
                "SELECT * FROM outbox WHERE chat_id=? AND state!='sent' ORDER BY id",
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
        elif action == "context":
            item_id = number(form, "item_id", 1, True)
            if not con.execute(
                "SELECT 1 FROM chat_items WHERE chat_id=? AND item_id=?",
                (chat_id, item_id),
            ).fetchone():
                raise HTTPException(403, "Позиция не принадлежит чату")
            scenarios.takeover(con, chat_id, actor["id"])
            con.execute("UPDATE chats SET active_item=? WHERE id=?", (item_id, chat_id))
            if not con.execute(
                "SELECT 1 FROM instances WHERE item_id=?", (item_id,)
            ).fetchone():
                scenarios.start(con, item_id, chat_id)
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
                raise scenarios.Invalid("Сначала выберите товарную позицию")
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
    if section not in {"overview", "ozon", "retailcrm", "automation", "users"}:
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
        published_types = {
            row[0] for row in con.execute(
                "SELECT DISTINCT product_type FROM scenarios WHERE published IS NOT NULL"
            )
        }
        current_settings = db.config(con)
        retailcrm_config = retailcrm.integration(con)
        retailcrm_capabilities = (
            json.loads(retailcrm_config["capabilities"])
            if retailcrm_config
            else {}
        )
        retailcrm_job = con.execute(
            "SELECT id,state,error,created_at,finished_at FROM jobs "
            "WHERE kind='retailcrm_probe' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        onboarding = {
            "connection": bool(accounts),
            "capabilities": any(
                all(caps.get(key) for key in ("account", "orders", "list", "history"))
                for caps in capabilities.values()
            ),
            "mappings": bool(con.execute("SELECT 1 FROM mappings LIMIT 1").fetchone()),
            "scenarios": published_types == set(scenarios.TYPES),
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
            retailcrm_config=retailcrm_config,
            retailcrm_capabilities=retailcrm_capabilities,
            retailcrm_job=retailcrm_job,
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
            "SELECT 1 FROM jobs WHERE kind='retailcrm_create' AND state='running'"
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
                "capabilities=?,checked_at=?,error=?,revision=revision+1 WHERE id=1",
                (
                    base_url,
                    site,
                    secret,
                    "{}" if changed else current["capabilities"],
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
        settings["handoff_hours"] = number(
            form, "handoff_hours", 0, optional=True
        )
        settings["send_enabled"] = form.get("send_enabled") == "on"
        settings["start_enabled"] = form.get("start_enabled") == "on"
        settings["automation_enabled"] = form.get("automation_enabled") == "on"
        settings["manager_resume"] = form.get("manager_resume") == "on"
        for key, options in {
            "manager_scope": {"all", "assigned"},
            "timer_origin": {"confirmation", "review"},
        }.items():
            value = form.get(key)
            if value not in options:
                raise scenarios.Invalid("Выберите один из предложенных вариантов")
            settings[key] = value
        settings["start_statuses"] = [
            value.strip()
            for value in str(form.get("start_statuses", "")).splitlines()
            if value.strip()
        ]
        if settings["start_enabled"] and not settings["start_statuses"]:
            raise scenarios.Invalid(
                "Укажите статусы заказов Ozon, для которых разрешено начинать диалог"
            )
        if settings["automation_enabled"]:
            if not settings["send_enabled"]:
                raise scenarios.Invalid(
                    "Перед автоматизацией включите отправку и подтвердите её в тестовом чате"
                )
            account_rows = con.execute(
                "SELECT capabilities FROM accounts WHERE checked_at IS NOT NULL AND error IS NULL"
            ).fetchall()
            required_capabilities = [
                "account",
                "orders",
                "list",
                "history",
                "send",
                "attachments",
            ]
            if settings["start_enabled"]:
                required_capabilities.append("start")
            if not any(
                all(json.loads(row[0]).get(key) for key in required_capabilities)
                for row in account_rows
            ):
                raise scenarios.Invalid(
                    "Для автоматизации подтвердите кабинет, заказы, чтение и отправку чатов, получение вложений"
                    + (
                        " и создание чата"
                        if settings["start_enabled"]
                        else ""
                    )
                )
            published_types = {
                row[0] for row in con.execute(
                    "SELECT DISTINCT product_type FROM scenarios WHERE published IS NOT NULL"
                )
            }
            if published_types != set(scenarios.TYPES):
                raise scenarios.Invalid(
                    "Опубликуйте проверенные сценарии для всех трёх типов товаров"
                )
            if not con.execute("SELECT 1 FROM mappings LIMIT 1").fetchone():
                raise scenarios.Invalid("Добавьте хотя бы одну привязку SKU к сценарию")
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
            "INSERT INTO users VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,role=excluded.role",
            (uid, name, role),
        )
        if enabled:
            con.execute("DELETE FROM revoked_users WHERE user_id=?", (uid,))
        else:
            con.execute("INSERT OR IGNORE INTO revoked_users VALUES(?)", (uid,))
        db.audit(con, actor["id"], "user.updated", "user", uid)
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
        }:
            raise scenarios.Invalid("История тестового диалога повреждена")
        if event["kind"] == "text":
            value = str(event.get("value", "")).strip()
            if not value or len(value) > policy.MESSAGE_MAX_CHARS:
                raise scenarios.Invalid("Тестовое сообщение должно содержать от 1 до 1000 символов")
            clean.append({"kind": "text", "value": value})
        elif event["kind"] == "photo":
            clean.append({"kind": "photo", "count": 1})
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
        return render(
            request,
            con,
            actor,
            "catalog.html",
            accounts=con.execute("SELECT id,name FROM accounts").fetchall(),
            mappings=con.execute(
                "SELECT m.*,a.name AS account_name,s.name AS scenario_name "
                "FROM mappings m JOIN accounts a ON a.id=m.account_id "
                "JOIN scenarios s ON s.id=m.scenario_id ORDER BY a.name,m.sku"
            ).fetchall(),
            records=con.execute("SELECT * FROM scenarios").fetchall(),
            catalog=con.execute("SELECT * FROM templates").fetchall(),
        )


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
        con.execute(
            "INSERT INTO mappings(account_id,sku,product_type,scenario_id,template_ids) VALUES(?,?,?,?,?) ON CONFLICT(account_id,sku) DO UPDATE SET product_type=excluded.product_type,scenario_id=excluded.scenario_id,template_ids=excluded.template_ids",
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
            "UPDATE items SET mapping_id=? WHERE account_id=? AND sku=? AND id NOT IN (SELECT item_id FROM instances)",
            (mid, account_id, sku),
        )
        db.audit(con, actor["id"], "mapping.updated", "mapping", mid)
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
                "SELECT i.*,a.name AS account_name,s.status AS brief_status,s.id AS instance_id "
                "FROM items i JOIN accounts a ON a.id=i.account_id "
                "LEFT JOIN instances s ON s.item_id=i.id ORDER BY i.id DESC"
            ).fetchall(),
            chats=con.execute(
                "SELECT c.*,a.name AS account_name FROM chats c "
                "JOIN accounts a ON a.id=c.account_id ORDER BY c.updated_at DESC"
            ).fetchall(),
            users=con.execute("SELECT * FROM users WHERE role='manager'").fetchall(),
        )


@app.post("/links")
async def link_item(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        chat = get_record(con, "chats", number(form, "chat_id", 1, True))
        item = get_record(con, "items", number(form, "item_id", 1, True))
        if chat["account_id"] != item["account_id"]:
            raise scenarios.Invalid("Кабинеты заказа и чата различаются")
        existing = con.execute(
            "SELECT chat_id FROM instances WHERE item_id=?", (item["id"],)
        ).fetchone()
        if existing and existing[0] != chat["id"]:
            raise scenarios.Invalid("Позиция уже обрабатывается в другом чате")
        con.execute(
            "INSERT OR IGNORE INTO chat_items VALUES(?,?)", (chat["id"], item["id"])
        )
        manager = str(form.get("manager_id", ""))
        if manager:
            get_record(con, "users", manager)
            con.execute(
                "INSERT OR IGNORE INTO assignments VALUES(?,?)", (chat["id"], manager)
            )
        db.audit(con, actor["id"], "chat.linked", "item", item["id"], chat["id"])
    return redirect(f"/chats/{chat['id']}")


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
                "SELECT * FROM messages WHERE chat_id=? AND external_id=? AND actor='seller' AND body=?",
                (record["chat_id"], external_id, record["body"]),
            ).fetchone()
            if not match:
                raise scenarios.Invalid(
                    "В синхронизированной истории нет соответствующего сообщения продавца"
                )
            con.execute(
                "UPDATE outbox SET state='sent',external_id=?,error=NULL WHERE id=?",
                (external_id, outbox_id),
            )
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
        if record["kind"] == "retailcrm_create":
            raise scenarios.Invalid(
                "Ошибка создания сделки уже обработана веткой сценария. Повторите действие из RetailCRM или запустите новый сценарий после исправления причины."
            )
        con.execute(
            "UPDATE jobs SET state='pending',error=NULL,finished_at=NULL WHERE id=?",
            (job_id,),
        )
        db.audit(con, actor["id"], "integration.retry_requested", "job", job_id)
    return redirect("/audit")
