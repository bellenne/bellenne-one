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

from . import db, media, scenarios, security

BASE = Path(__file__).parent
PREFIX = os.environ.get("MODULE_PREFIX", "/folio").rstrip("/")
ROOT = BASE.parents[2]
PULSE = ROOT / "apps/pulse/app"
# Docker uses copies of existing shared assets; local execution reads originals.
SHARED = BASE / "shared" if (BASE / "shared").exists() else PULSE / "static"
templates = Jinja2Templates(
    directory=[str(BASE / "templates"), str(PULSE / "templates")]
)
templates.env.filters["json"] = json.loads


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


def required(form, key):
    value = str(form.get(key, "")).strip()
    if not value:
        raise scenarios.Invalid(f"Заполните поле {key}")
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
        raise scenarios.Invalid(f"Некорректное число: {key}") from None


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
            "SELECT c.* FROM chats c WHERE "
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
                "SELECT * FROM messages WHERE chat_id=? ORDER BY created_at,id",
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
            limit = settings.get("message_max_chars")
            if not settings.get("send_enabled") or not limit or len(text) > limit:
                raise scenarios.Invalid(
                    "Отправка отключена или превышен настроенный лимит текста"
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
            maximum = db.config(con).get("media_max_bytes")
            if not maximum or not hasattr(upload, "read"):
                raise scenarios.Invalid("Настройте лимит и выберите изображение")
            content = await upload.read(maximum + 1)
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
def settings_page(request: Request):
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        accounts = con.execute(
            "SELECT id,name,client_id,capabilities,checked_at,error FROM accounts"
        ).fetchall()
        return render(
            request,
            con,
            actor,
            "settings.html",
            accounts=accounts,
            users=con.execute(
                "SELECT u.*,NOT EXISTS(SELECT 1 FROM revoked_users r WHERE r.user_id=u.id) AS enabled FROM users u"
            ).fetchall(),
        )


@app.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        settings = dict(db.config(con))
        for key in (
            "handoff_hours",
            "http_timeout",
            "request_interval",
            "sync_minutes",
        ):
            settings[key] = number(
                form, key, 0 if key == "handoff_hours" else 0.001, optional=True
            )
        for key in ("media_max_bytes", "message_max_chars"):
            settings[key] = number(form, key, 1, True, optional=True)
        for key in (
            "send_enabled",
            "start_enabled",
            "automation_enabled",
            "sync_enabled",
            "manager_resume",
        ):
            settings[key] = form.get(key) == "on"
        for key, options in {
            "manager_scope": {"all", "assigned"},
            "timer_origin": {"confirmation", "review"},
        }.items():
            value = form.get(key)
            if value not in options:
                raise scenarios.Invalid(f"Выберите {key}")
            settings[key] = value
        since = str(form.get("sync_since", "")).strip()
        if since:
            try:
                parsed = datetime.fromisoformat(since)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                settings["sync_since"] = parsed.isoformat()
            except ValueError:
                raise scenarios.Invalid("Неверная дата начала синхронизации") from None
        settings["media_mimes"] = [
            m
            for m in form.getlist("media_mimes")
            if m in {"image/jpeg", "image/png", "image/webp"}
        ]
        settings["start_statuses"] = [
            value.strip()
            for value in str(form.get("start_statuses", "")).splitlines()
            if value.strip()
        ]
        if settings["start_enabled"] and not settings["start_statuses"]:
            raise scenarios.Invalid(
                "Укажите статусы заказов Ozon, для которых разрешено начинать диалог"
            )
        settings["media_hosts"] = [
            h.strip().lower()
            for h in str(form.get("media_hosts", "")).splitlines()
            if h.strip()
        ]
        if settings["send_enabled"] and not settings.get("message_max_chars"):
            raise scenarios.Invalid("Перед включением отправки задайте лимит текста")
        if settings["sync_enabled"] and not all(
            settings.get(k)
            for k in ("sync_minutes", "sync_since", "http_timeout", "request_interval")
        ):
            raise scenarios.Invalid("Заполните дату начала, период и HTTP-лимиты")
        con.execute("UPDATE settings SET value=? WHERE id=1", (db.dump(settings),))
        db.audit(con, actor["id"], "settings.updated", "settings", 1)
    return redirect("/settings")


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
            con.execute(
                "UPDATE accounts SET name=?,client_id=?,secret=?,capabilities='{}',checked_at=NULL,error=NULL,revision=revision+1 WHERE id=?",
                (name, client, secret, aid),
            )
        else:
            if not key:
                raise scenarios.Invalid("Введите API-ключ")
            aid = con.execute(
                "INSERT INTO accounts(name,client_id,secret) VALUES(?,?,?)",
                (name, client, security.cipher().encrypt(key.encode()).decode()),
            ).lastrowid
        db.audit(con, actor["id"], "account.saved", "account", aid)
    return redirect("/settings")


@app.post("/accounts/{account_id}/{action}")
async def account_action(request: Request, account_id: int, action: str):
    form = await request.form()
    security.csrf(request, form.get("csrf"))
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        get_record(con, "accounts", account_id)
        if action not in {"probe", "sync"}:
            raise HTTPException(404)
        db.job(con, action, account_id)
        db.audit(
            con, actor["id"], f"integration.queued_{action}", "account", account_id
        )
    return redirect("/audit")


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
            raise scenarios.Invalid("Допустимы только admin и manager")
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
    return redirect("/settings")


@app.get("/scenarios")
@app.get("/scenarios/{scenario_id}")
def scenario_page(request: Request, scenario_id: int | None = None):
    with db.transaction() as con:
        actor = security.user(request, con, admin=True)
        record = get_record(con, "scenarios", scenario_id) if scenario_id else None
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
    nodes = []
    for i, node_id in enumerate(form.getlist("node_id")):

        def value(key, index=i):
            values = form.getlist(key)
            return str(values[index]).strip() if index < len(values) else ""

        node = {
            "id": str(node_id).strip(),
            "kind": value("kind"),
            "text": value("text"),
            "field": value("field"),
            "next": value("next"),
            "otherwise": value("otherwise"),
            "error": value("error"),
            "equals": value("equals"),
            "accept": value("accept"),
            "required": value("required") == "yes",
            "choices": [v.strip() for v in value("choices").splitlines() if v.strip()],
        }
        for key in ("min", "max", "max_length"):
            node[key] = number({key: value(key)}, key, 1, True, True)
        nodes.append(node)
    return {"nodes": nodes}


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
            if action == "validate":
                scenarios.validate(graph, record["product_type"])
            con.execute(
                "UPDATE scenarios SET draft=?,revision=revision+1 WHERE id=?",
                (db.dump(graph), scenario_id),
            )
        elif action == "preview":
            graph = json.loads(record["draft"])
            scenarios.validate(graph, record["product_type"])
            from .simulation import simulate

            result = simulate(
                graph, record["product_type"], str(form.get("answers", "")).splitlines()
            )
            return render(
                request, con, actor, "preview.html", result=result, record=record
            )
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
            mappings=con.execute("SELECT * FROM mappings").fetchall(),
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
        offer = required(form, "offer_id")
        con.execute(
            "INSERT INTO mappings(account_id,offer_id,product_type,scenario_id,template_ids) VALUES(?,?,?,?,?) ON CONFLICT(account_id,offer_id) DO UPDATE SET product_type=excluded.product_type,scenario_id=excluded.scenario_id,template_ids=excluded.template_ids",
            (
                account_id,
                offer,
                scenario["product_type"],
                scenario["id"],
                db.dump(tids),
            ),
        )
        mid = con.execute(
            "SELECT id FROM mappings WHERE account_id=? AND offer_id=?",
            (account_id, offer),
        ).fetchone()[0]
        con.execute(
            "UPDATE items SET mapping_id=? WHERE account_id=? AND offer_id=? AND id NOT IN (SELECT item_id FROM instances)",
            (mid, account_id, offer),
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
            maximum = db.config(con).get("media_max_bytes")
            if not maximum:
                raise scenarios.Invalid("Сначала настройте лимит файлов")
            mid = media.store(con, await upload.read(maximum + 1), db.config(con))
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
                "SELECT i.*,s.status AS brief_status,s.id AS instance_id FROM items i LEFT JOIN instances s ON s.item_id=i.id ORDER BY i.id DESC"
            ).fetchall(),
            chats=con.execute("SELECT * FROM chats").fetchall(),
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
                "SELECT id,kind,account_id,state,error,created_at,finished_at FROM jobs ORDER BY id DESC LIMIT 100"
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
        if external_id:
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
        elif form.get("cancel") == "yes":
            con.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (outbox_id,))
        else:
            raise scenarios.Invalid(
                "Укажите подтверждённый message_id или закройте попытку без повтора"
            )
        db.audit(
            con,
            actor["id"],
            "outbound.reconciled",
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
        con.execute(
            "UPDATE jobs SET state='pending',error=NULL,finished_at=NULL WHERE id=?",
            (job_id,),
        )
        db.audit(con, actor["id"], "integration.retry_requested", "job", job_id)
    return redirect("/audit")
