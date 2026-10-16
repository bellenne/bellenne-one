"""Durable, idempotent order delivery; mounted files never become public paths."""
import hashlib
import ipaddress
import json
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx

from . import db, security


def config(con, *, secrets=False):
    row = con.execute("SELECT value FROM order_webhook_settings WHERE id=1").fetchone()
    value = json.loads(row[0]) if row else {}
    for key in ("send_token", "download_token"):
        encoded = value.pop(key, "")
        value[key + "_set"] = bool(encoded)
        if secrets:
            value[key] = security.cipher().decrypt(encoded.encode()).decode() if encoded else ""
    return value


def https_url(value):
    value = value.strip().rstrip("/")
    p = urlsplit(value)
    if (p.scheme != "https" or not p.hostname or p.username or p.password
            or p.query or p.fragment or p.port not in (None, 443)):
        raise ValueError("Требуется HTTPS URL без токена, параметров и нестандартного порта")
    if p.hostname == "localhost" or p.hostname.endswith(".localhost"):
        raise ValueError("Нужен публичный адрес сервера")
    try:
        address = ipaddress.ip_address(p.hostname)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("Нужен публичный адрес сервера")
    return value


def save(con, form):
    old = config(con, secrets=True)
    value = {key: str(form.get(key) or "").strip() for key in
             ("url", "public_url", "worker_root", "mount_root")}
    value["url"] = https_url(value["url"])
    value["public_url"] = https_url(value["public_url"])
    if not value["worker_root"] or not Path(value["mount_root"]).is_absolute():
        raise ValueError("Укажите корень печатных файлов воркера и абсолютный путь монтирования в Core")
    for key in ("send_token", "download_token"):
        token = str(form.get(key) or old.get(key) or "").strip()
        if len(token) < 32 or any(c.isspace() for c in token):
            raise ValueError("Каждый токен должен содержать минимум 32 символа без пробелов")
        value[key] = security.cipher().encrypt(token.encode()).decode()
    if (form.get("send_token") or old.get("send_token")) == (form.get("download_token") or old.get("download_token")):
        raise ValueError("Для отправки и скачивания используйте разные токены")
    con.execute("INSERT INTO order_webhook_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                (db.dump(value),))


def mounted_file(settings, filename):
    source = PurePosixPath(str(filename).replace("\\", "/"))
    root = PurePosixPath(settings["worker_root"].replace("\\", "/"))
    try:
        relative = source.relative_to(root)
    except ValueError:
        raise ValueError("Печатный файл вне настроенного корня воркера") from None
    if not relative.parts or any(p in {"..", "."} or ":" in p for p in relative.parts):
        raise ValueError("Недопустимый путь печатного файла")
    mount = Path(settings["mount_root"]).resolve()
    path = (mount / Path(*relative.parts)).resolve()
    if not path.is_relative_to(mount) or not path.is_file():
        raise ValueError("Печатный файл не найден в примонтированной папке")
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".pdf"}:
        raise ValueError("Неподдерживаемый формат печатного файла")
    return path


def enqueue(con, instance_id, node_id):
    row = con.execute("SELECT * FROM order_webhook_actions WHERE instance_id=? AND node_id=?",
                      (instance_id, node_id)).fetchone()
    if row:
        return row
    # Bind the last actually sent preview, not a newer unapproved generation.
    job = con.execute("SELECT j.id FROM instances i JOIN outbox o ON o.chat_id=i.chat_id "
                      "JOIN media m ON m.id=o.media_id AND m.item_id=i.item_id "
                      "LEFT JOIN image_jobs j ON j.preview_media_id=o.media_id AND j.item_id=i.item_id AND j.state='completed' "
                      "WHERE i.id=? AND o.kind='file' AND o.state='sent' "
                      "ORDER BY o.id DESC,j.id DESC LIMIT 1",
                      (instance_id,)).fetchone()
    event = str(uuid.uuid4())
    con.execute("INSERT INTO order_webhook_actions(id,instance_id,node_id,image_job_id,due_at,created_at) VALUES(?,?,?,?,?,?)",
                (event, instance_id, node_id, job[0] if job else None, db.now(), db.now()))
    db.audit(con, "system", "order_webhook.queued", "order_webhook", event)
    return con.execute("SELECT * FROM order_webhook_actions WHERE id=?", (event,)).fetchone()


def artifact_path(event):
    # IDs originate in our database, never from a caller-supplied filename.
    if str(uuid.UUID(event)) != event:
        raise ValueError("Некорректный ID файла")
    return db.DATA / "order_webhook_files" / event


def prepare(action, settings, item, job):
    if not job:
        raise ValueError("Нет отправленного макета с печатным файлом для этой позиции")
    if not item["order_number"] or not item["posting"] or item["quantity"] <= 0:
        raise ValueError("Не заполнены номер заказа, отправление или количество")
    source = mounted_file(settings, job["print_file"])
    target = artifact_path(action["id"])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    digest, size = hashlib.sha256(), 0
    # Freeze bytes before sending; subsequent changes to the mount cannot change a delivery.
    with source.open("rb") as src, temporary.open("wb") as dst:
        os.chmod(temporary, 0o600)
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
            dst.write(chunk)
    if not size:
        raise ValueError("Печатный файл пуст")
    temporary.replace(target)
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".tif": "image/tiff", ".tiff": "image/tiff", ".pdf": "application/pdf"}[source.suffix.lower()]
    return {"version": 1, "order": {
        "source": "ozon", "seller_account_id": str(item["account_id"]),
        "order_number": item["order_number"], "posting_number": item["posting"],
        "item_id": str(item["id"]), "offer_id": item["offer_id"], "type": "Картина",
        "width_cm": job["width_cm"], "height_cm": job["height_cm"], "quantity": item["quantity"],
    }, "print_file": {"id": action["id"], "url": settings["public_url"] + "/integration-files/" + action["id"],
                       "filename": source.name, "mime_type": mime, "size_bytes": size, "sha256": digest.hexdigest()}}


def send(url, token, payload, event, *, transport=None):
    https_url(url)
    host = urlsplit(url).hostname
    if transport is None and any(not ipaddress.ip_address(x[4][0]).is_global for x in socket.getaddrinfo(host, 443)):
        raise ValueError("Webhook разрешается в непубличный адрес")
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False, transport=transport) as client:
        response = client.post(url, content=payload.encode(), headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json", "Idempotency-Key": event})
    return response


def tick(sender=send):
    from . import scenarios
    with db.transaction() as con:
        action = con.execute("SELECT * FROM order_webhook_actions WHERE state IN ('pending','sending') AND due_at<=? ORDER BY created_at LIMIT 1",
                             (db.now(),)).fetchone()
        if not action:
            return False
        instance = con.execute("SELECT * FROM instances WHERE id=?", (action["instance_id"],)).fetchone()
        item = con.execute("SELECT * FROM items WHERE id=?", (instance["item_id"],)).fetchone()
        chat = con.execute("SELECT * FROM chats WHERE id=?", (instance["chat_id"],)).fetchone()
        if item["external_status"] == "cancelled":
            con.execute("UPDATE order_webhook_actions SET state='failed',error='Заказ отменён' WHERE id=?", (action["id"],))
            return True
        if not action["delivery_only"] and (chat["mode"] != "bot" or chat["active_item"] != item["id"] or instance["node"] != action["node_id"]):
            con.execute("UPDATE order_webhook_actions SET due_at=? WHERE id=?",
                        ((datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(), action["id"]))
            return False
        lease = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        con.execute("UPDATE order_webhook_actions SET state='sending',due_at=?,attempts=attempts+1 WHERE id=?", (lease, action["id"]))
        job = con.execute("SELECT * FROM image_jobs WHERE id=?", (action["image_job_id"],)).fetchone()
    state, error, delay = "failed", None, 60
    try:
        with db.transaction() as con:
            settings = config(con, secrets=True)
        if not settings.get("url"):
            raise ValueError("Настройте подключение вебхука в Folio")
        payload = action["payload"]
        if not payload:
            payload = db.dump(prepare(action, settings, item, job))
            with db.transaction() as con:
                con.execute("UPDATE order_webhook_actions SET payload=?,destination=? WHERE id=?", (payload, settings["url"], action["id"]))
        if action["destination"] and action["destination"] != settings["url"]:
            raise ValueError("Адрес подключения изменился после первой попытки; восстановите адрес перед повтором")
        # Serialize takeover/cancellation with the actual send, after potentially
        # slow copying. Same transaction boundary as the chat outbox sender.
        with db.transaction() as con:
            current = con.execute("SELECT c.mode,c.active_item,i.node,t.external_status FROM instances i "
                                  "JOIN chats c ON c.id=i.chat_id JOIN items t ON t.id=i.item_id WHERE i.id=?",
                                  (action["instance_id"],)).fetchone()
            if current["external_status"] == "cancelled":
                raise ValueError("Заказ отменён")
            if not action["delivery_only"] and (current["mode"] != "bot" or current["active_item"] != item["id"] or current["node"] != action["node_id"]):
                con.execute("UPDATE order_webhook_actions SET state='pending',due_at=? WHERE id=?", (lease, action["id"]))
                return True
            response = sender(settings["url"], settings["send_token"], payload, action["id"])
        if response.status_code in {200, 202}:
            body = response.json()
            if isinstance(body, dict) and body.get("accepted") is True:
                state = "sent"
            else:
                error = "Laravel вернул некорректное подтверждение приёма"
        else:
            error = f"Laravel: HTTP {response.status_code}"
            if response.status_code in {408, 429} or response.status_code >= 500:
                state = "pending"
                try:
                    delay = max(delay, int(response.headers.get("Retry-After", "0")))
                except ValueError:
                    pass
    except (httpx.TransportError, OSError):
        state, error = "pending", "Не удалось подтвердить доставку вебхука; повтор с тем же ID"
    except ValueError as exc:
        error = str(exc) if not isinstance(exc, json.JSONDecodeError) else "Некорректный JSON ответа Laravel"
    except Exception:
        error = "Ошибка подготовки вебхука; проверьте настройки интеграции"
    if state == "pending" and action["attempts"] >= 4:
        state = "failed"
    due = (datetime.now(timezone.utc) + timedelta(seconds=max(delay, 60 * 2 ** min(action["attempts"], 4)))).isoformat()
    with db.transaction() as con:
        con.execute("UPDATE order_webhook_actions SET state=?,error=?,due_at=? WHERE id=?", (state, error, due, action["id"]))
        db.audit(con, "system", "order_webhook." + state, "order_webhook", action["id"], instance["chat_id"])
        if state in {"sent", "failed"} and not action["delivery_only"]:
            scenarios.advance(con, action["instance_id"])
    return True
