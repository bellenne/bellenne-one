"""Durable metadata-only delivery to MES; print files stay on the worker."""
import ipaddress
import json
import math
import re
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

from . import db, security


def config(con, *, secrets=False):
    row = con.execute("SELECT value FROM order_webhook_settings WHERE id=1").fetchone()
    value = json.loads(row[0]) if row else {}
    value.pop("download_token", None)
    value.pop("public_url", None)
    for key in ("worker_root", "mount_root", "production_root"):
        value.pop(key, None)
    for key in ("send_token",):
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
    value = {"url": str(form.get("url") or "").strip()}
    value["url"] = https_url(value["url"])
    parsed = urlsplit(value["url"])
    if parsed.path in ("", "/"):
        value["url"] += "/api/v1/folio/orders"
    elif parsed.path != "/api/v1/folio/orders":
        raise ValueError("Укажите адрес MES или полный путь /api/v1/folio/orders")
    value["shops"] = {}
    for account in con.execute("SELECT id FROM accounts"):
        shop = str(form.get("shop_" + str(account["id"])) or "").strip()
        if shop:
            if not re.fullmatch(r"[1-9][0-9]*", shop) or len(shop) > 18:
                raise ValueError("ID магазина MES должен быть положительным целым числом")
            value["shops"][str(account["id"])] = int(shop)
    for key in ("send_token",):
        token = str(form.get(key) or old.get(key) or "").strip()
        if len(token) < 32 or any(c.isspace() for c in token):
            raise ValueError("Каждый токен должен содержать минимум 32 символа без пробелов")
        value[key] = security.cipher().encrypt(token.encode()).decode()
    con.execute("INSERT INTO order_webhook_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                (db.dump(value),))


def print_filename(value):
    """Extract metadata, without accessing a Windows/UNC path from Core."""
    path = str(value or "").replace("\\", "/")
    if not path.strip() or path.endswith("/") or any(ord(c) < 32 for c in path):
        raise ValueError("Воркер не сообщил имя сохранённого печатного файла")
    name = PurePosixPath(path).name
    if name in {"", ".", ".."} or ":" in name or len(name) > 255:
        raise ValueError("Недопустимое имя печатного файла")
    return name


def bound_job(con, instance, forced):
    # Bind the last actually sent preview, not a newer unapproved generation.
    sent = con.execute("SELECT j.id FROM outbox o JOIN media m ON m.id=o.media_id AND m.item_id=? "
                       "LEFT JOIN image_jobs j ON j.preview_media_id=o.media_id AND j.item_id=? AND j.state='completed' "
                       "WHERE o.chat_id=? AND o.kind='file' AND o.state='sent' "
                       "ORDER BY o.id DESC,j.id DESC LIMIT 1",
                       (instance["item_id"], instance["item_id"], instance["chat_id"])).fetchone()
    if sent or not forced:
        # A manager's own file has no print result: never print another mockup instead.
        return sent[0] if sent else None
    # The manager moved the order on before any preview reached the buyer.
    latest = con.execute("SELECT id FROM image_jobs WHERE item_id=? AND state='completed' "
                         "AND print_file IS NOT NULL AND preview_media_id IS NOT NULL ORDER BY id DESC LIMIT 1",
                         (instance["item_id"],)).fetchone()
    return latest[0] if latest else None


def force(con, row, instance, node_id, armed):
    """A manager's decision outranks the approval, chat mode and step gates."""
    if row["state"] == "sent" or row["protocol"] != 2:
        return row
    if row["state"] == "failed":
        # A conflict needs reconciliation with MES; "armed" is spent by one
        # explicit manager action, so the error branch cannot loop into a resend.
        if not armed or row["http_code"] == 409:
            return row
        con.execute("UPDATE order_webhook_actions SET state='pending',error=NULL,due_at=?,delivery_only=0 WHERE id=?",
                    (db.now(), row["id"]))
    job = row["image_job_id"] if row["payload"] else bound_job(con, instance, True)
    con.execute("UPDATE order_webhook_actions SET manager_forced=1,node_id=?,image_job_id=? WHERE id=?",
                (node_id, job, row["id"]))
    db.audit(con, "system", "order_webhook.manager_forced", "order_webhook", row["id"], instance["chat_id"])
    return con.execute("SELECT * FROM order_webhook_actions WHERE id=?", (row["id"],)).fetchone()


def enqueue(con, instance_id, node_id):
    from . import scenarios
    instance = con.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
    armed = instance["manager_forced"] == scenarios.MANAGER_ARMED
    if armed:
        con.execute("UPDATE instances SET manager_forced=1 WHERE id=?", (instance_id,))
    forced = bool(instance["manager_forced"]) or scenarios.manager_approved(con, instance)
    # One immutable event per position, even when a graph has another webhook
    # node. A new UUID must never be used to bypass a prior acceptance/conflict.
    row = con.execute("SELECT * FROM order_webhook_actions WHERE instance_id=? ORDER BY created_at LIMIT 1",
                      (instance_id,)).fetchone()
    if row:
        return force(con, row, instance, node_id, armed) if forced else row
    event = str(uuid.uuid4())
    con.execute("INSERT INTO order_webhook_actions(id,instance_id,node_id,image_job_id,due_at,created_at,protocol,manager_forced) VALUES(?,?,?,?,?,?,2,?)",
                (event, instance_id, node_id, bound_job(con, instance, forced), db.now(), db.now(), int(forced)))
    db.audit(con, "system", "order_webhook.queued", "order_webhook", event)
    return con.execute("SELECT * FROM order_webhook_actions WHERE id=?", (event,)).fetchone()


def prepare(action, settings, item, job):
    if not job or job["state"] != "completed":
        raise ValueError("Нет макета с печатным файлом для этой позиции. Файл, отправленный менеджером вручную, не связан с печатным результатом")
    if not item["order_number"] or not item["posting"] or item["quantity"] <= 0:
        raise ValueError("Не заполнены номер заказа, отправление или количество")
    shop = settings.get("shops", {}).get(str(item["account_id"]))
    if type(shop) is not int or shop <= 0:
        raise ValueError("Укажите ID магазина MES для этого кабинета Ozon")
    for number in (job["width_cm"], job["height_cm"]):
        if not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
            raise ValueError("Размеры макета должны быть положительными числами")
    filename = print_filename(job["print_file"])
    payload = {"version": 1, "order": {
        "source": "ozon", "shop_id": shop,
        "order_number": item["order_number"], "posting_number": item["posting"],
        "item_id": "folio-item-" + str(item["id"]), "offer_id": item["offer_id"],
        "width_cm": job["width_cm"], "height_cm": job["height_cm"], "quantity": item["quantity"],
    }, "print_file": {"id": action["id"], "filename": filename}}
    for value in (*payload["order"].values(), filename):
        if isinstance(value, str) and (not value.strip() or len(value) > 255):
            raise ValueError("Строки заказа и имя файла должны содержать от 1 до 255 символов")
    if type(item["quantity"]) is not int or item["quantity"] <= 0:
        raise ValueError("Количество должно быть положительным целым числом")
    if len(db.dump(payload).encode("utf-8")) > 64 * 1024:
        raise ValueError("Тело вебхука превышает 64 KiB")
    return payload


def send(url, token, payload, event, *, transport=None):
    https_url(url)
    host = urlsplit(url).hostname
    if transport is None and any(not ipaddress.ip_address(x[4][0]).is_global for x in socket.getaddrinfo(host, 443)):
        raise ValueError("Webhook разрешается в непубличный адрес")
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False, transport=transport) as client:
        response = client.post(url, content=payload.encode(), headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "Accept": "application/json", "Idempotency-Key": event})
    return response


def tick(sender=send):
    from . import scenarios
    with db.transaction() as con:
        if config(con).get("paused_reason"):
            return False
        action = con.execute("SELECT * FROM order_webhook_actions WHERE protocol=2 AND state IN ('pending','sending') AND due_at<=? ORDER BY created_at LIMIT 1",
                             (db.now(),)).fetchone()
        if not action:
            return False
        instance = con.execute("SELECT * FROM instances WHERE id=?", (action["instance_id"],)).fetchone()
        item = con.execute("SELECT * FROM items WHERE id=?", (instance["item_id"],)).fetchone()
        chat = con.execute("SELECT * FROM chats WHERE id=?", (instance["chat_id"],)).fetchone()
        if item["external_status"] == "cancelled":
            con.execute("UPDATE order_webhook_actions SET state='failed',error='Заказ отменён' WHERE id=?", (action["id"],))
            return True
        unattended = not action["delivery_only"] and not action["manager_forced"]
        if unattended and (chat["mode"] != "bot" or chat["active_item"] != item["id"] or instance["node"] != action["node_id"]):
            con.execute("UPDATE order_webhook_actions SET due_at=? WHERE id=?",
                        ((datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(), action["id"]))
            return False
        lease = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        con.execute("UPDATE order_webhook_actions SET state='sending',due_at=? WHERE id=?", (lease, action["id"]))
        job = con.execute("SELECT * FROM image_jobs WHERE id=?", (action["image_job_id"],)).fetchone()
    state, error, delay = "failed", None, (60, 120, 300, 900, 1800)[min(action["attempts"], 4)] if action["attempts"] < 5 else 3600
    code, error_code, external_id, pause = None, None, None, None
    print_files, placement_error = None, None
    try:
        with db.transaction() as con:
            settings = config(con, secrets=True)
        if not settings.get("url"):
            raise ValueError("Настройте подключение вебхука в Folio")
        from . import placement
        with db.transaction() as con:
            if job and not action["manager_forced"] and not placement.approved(con, instance["id"], job):
                raise ValueError("Выбранный макет не согласован покупателем")
        payload = action["payload"]
        if not payload:
            payload = db.dump(prepare(action, settings, item, job))
            with db.transaction() as con:
                con.execute("UPDATE order_webhook_actions SET payload=?,destination=? WHERE id=?", (payload, settings["url"], action["id"]))
        if action["destination"] and action["destination"] != settings["url"]:
            raise ValueError("Адрес подключения изменился после первой попытки; восстановите адрес перед повтором")
        # Serialize takeover/cancellation with the actual metadata send.
        with db.transaction() as con:
            current = con.execute("SELECT c.mode,c.active_item,i.node,t.external_status FROM instances i "
                                  "JOIN chats c ON c.id=i.chat_id JOIN items t ON t.id=i.item_id WHERE i.id=?",
                                  (action["instance_id"],)).fetchone()
            if current["external_status"] == "cancelled":
                raise ValueError("Заказ отменён")
            if unattended and (current["mode"] != "bot" or current["active_item"] != item["id"] or current["node"] != action["node_id"]):
                con.execute("UPDATE order_webhook_actions SET state='pending',due_at=? WHERE id=?", (lease, action["id"]))
                return True
            if config(con).get("paused_reason"):
                con.execute("UPDATE order_webhook_actions SET state='pending' WHERE id=?", (action["id"],))
                return True
            con.execute("UPDATE order_webhook_actions SET attempts=attempts+1,last_attempt_at=?,http_code=NULL WHERE id=?",
                        (db.now(), action["id"]))
            # Commit the attempt before I/O so a process crash retains its count.
        try:
            # Recheck under the same write lock as takeover/cancellation.
            # The attempt counter was committed before network I/O.
            with db.transaction() as con:
                current = con.execute("SELECT c.mode,c.active_item,i.node,t.external_status FROM instances i "
                                      "JOIN chats c ON c.id=i.chat_id JOIN items t ON t.id=i.item_id WHERE i.id=?",
                                      (action["instance_id"],)).fetchone()
                if current["external_status"] == "cancelled":
                    raise ValueError("Заказ отменён")
                if not action["manager_forced"] and not placement.approved(con, action["instance_id"], job):
                    raise ValueError("Согласованная версия макета изменилась")
                if config(con).get("paused_reason") or (unattended and
                        (current["mode"] != "bot" or current["active_item"] != item["id"] or current["node"] != action["node_id"])):
                    con.execute("UPDATE order_webhook_actions SET state='pending',due_at=? WHERE id=?", (lease, action["id"]))
                    return True
                response = sender(settings["url"], settings["send_token"], payload, action["id"])
        except (httpx.TransportError, OSError):
            state, error = "pending", "Не удалось подтвердить доставку вебхука; повтор с тем же ID"
            response = None
        if response is not None:
            code = response.status_code
            try:
                body = response.json()
            except ValueError:
                body = {}
            body = body if isinstance(body, dict) else {}
            detail = safe_error(body, settings["send_token"])
            if code in {200, 202}:
                external_id = body.get("external_order_id")
                if body.get("accepted") is True and isinstance(external_id, str) and external_id.strip() and len(external_id) <= 255:
                    state = "sent"
                    from . import placement
                    try:
                        print_files = placement.validate_files(body.get("print_files"), json.loads(payload)["order"]["quantity"])
                    except ValueError as exc:
                        placement_error = str(exc)
                    if action["external_order_id"] and action["external_order_id"] != external_id:
                        external_id = action["external_order_id"]
                        print_files = None
                        placement_error = "MES изменила external_order_id; требуется ручная сверка"
                else:
                    external_id = None
                    state, error = "pending", "MES вернула некорректное подтверждение; повтор с тем же ID"
            else:
                error = f"MES: HTTP {code}" + (": " + detail if detail else "")
                remote_code = body.get("code") or (body["error"].get("code") if isinstance(body.get("error"), dict) else body.get("error"))
                error_code = "CONFIGURATION_ERROR" if remote_code == "CONFIGURATION_ERROR" else None
                if code == 401:
                    pause = "Ошибка токена MES. Исправьте folio.send_token и сохраните подключение."
                elif code == 503 and error_code == "CONFIGURATION_ERROR":
                    pause = "Ошибка настройки MES. После исправления MES повторите доставку кнопкой ниже."
                elif code in {408, 429} or code >= 500:
                    state = "pending"
                if state == "pending":
                    try:
                        delay = max(delay, int(response.headers.get("Retry-After", "0")))
                    except ValueError:
                        pass
    except (httpx.TransportError, OSError):
        error = "Ошибка передачи данных в MES"
    except ValueError as exc:
        error = str(exc) if not isinstance(exc, json.JSONDecodeError) else "Некорректный JSON ответа Laravel"
    except Exception:
        error = "Ошибка подготовки вебхука; проверьте настройки интеграции"
    due = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
    with db.transaction() as con:
        if pause:
            raw = json.loads(con.execute("SELECT value FROM order_webhook_settings WHERE id=1").fetchone()[0])
            raw["paused_reason"] = pause
            raw["paused_code"] = "AUTH_ERROR" if code == 401 else "CONFIGURATION_ERROR"
            con.execute("UPDATE order_webhook_settings SET value=? WHERE id=1", (db.dump(raw),))
        con.execute("UPDATE order_webhook_actions SET state=?,error=?,due_at=?,http_code=?,external_order_id=COALESCE(?,external_order_id),error_code=? WHERE id=?",
                    (state, error, due, code, external_id, error_code, action["id"]))
        if state == "sent":
            from . import placement
            if print_files is not None:
                existing = con.execute("SELECT print_files FROM order_webhook_actions WHERE id=?", (action["id"],)).fetchone()[0]
                if existing and json.loads(existing) != print_files:
                    placement_error = "MES изменила print_files; требуется ручная сверка"
                else:
                    con.execute("UPDATE order_webhook_actions SET print_files=? WHERE id=?", (db.dump(print_files), action["id"]))
                    placement_error = placement.schedule(con, action, print_files)
            con.execute("UPDATE order_webhook_actions SET placement_error=? WHERE id=?", (placement_error, action["id"]))
        db.audit(con, "system", "order_webhook." + state, "order_webhook", action["id"], instance["chat_id"])
        if state in {"sent", "failed"} and not action["delivery_only"]:
            # A forced delivery may finish after the manager moved to another step.
            if con.execute("SELECT node FROM instances WHERE id=?", (action["instance_id"],)).fetchone()[0] == action["node_id"]:
                scenarios.advance(con, action["instance_id"])
    return True


def safe_error(body, token):
    """Only bounded textual diagnostics; never headers, HTML or raw payloads."""
    error = body.get("error")
    value = body.get("message") or (error.get("message") if isinstance(error, dict) else error)
    if not value and isinstance(body.get("errors"), dict):
        messages = []
        for field, details in body["errors"].items():
            details = details if isinstance(details, list) else [details]
            messages.extend(f"{field}: {detail}" for detail in details if isinstance(detail, str))
        value = "; ".join(messages)
    if not isinstance(value, str):
        return ""
    value = value.replace(token, "[скрыто]") if token else value
    value = re.sub(r"(?i)bearer\s+\S+", "Bearer [скрыто]", value)
    return value[:1000]
