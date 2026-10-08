"""Durable Folio-side queue and authenticated FolioImageWorker adapter."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import uuid
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image

from . import db, security

PROGRESS = {"queued", "generating", "waiting_topaz", "composing", "webhook_pending"}
TERMINAL = {"completed", "failed"}
POLL_SECONDS = 30
STATE_LABELS = {
    "pending": "В очереди", "submitting": "Передаётся воркеру",
    "unknown": "Результат передачи не подтверждён", "queued": "Принято воркером",
    "generating": "Генерация", "waiting_topaz": "Ожидание Topaz",
    "composing": "Подготовка печатного файла", "webhook_pending": "Ожидание уведомления",
    "completed": "Готово", "failed": "Ошибка",
}
ERROR_LABELS = {
    "image_worker_submission_unknown": "Не удалось подтвердить приём задания воркером. Не отправляйте его повторно без проверки воркера.",
    "image_worker_status_unavailable": "Не удалось обновить состояние воркера. Folio повторит проверку.",
    "image_worker_http_404": "Воркер не нашёл задание с сохранённым ID. Проверьте его хранилище.",
    "image_worker_media_missing": "Исходное фото больше недоступно.",
    "image_worker_response_invalid": "Воркер вернул неожиданный ответ. Folio не повторяет создание задания.",
    "image_worker_dimensions_invalid": "Размер в артикуле некорректен. Исправьте артикул или передайте заказ менеджеру.",
    "image_worker_photo_ambiguous": "В брифе несколько фотографий. Выбор исходника требует ручной проверки.",
    "image_worker_order_cancelled": "Заказ отменён до передачи задания воркеру.",
}


class ImageTaskError(RuntimeError):
    def __init__(self, code: str, *, unknown: bool = False):
        super().__init__(code)
        self.code = code
        self.unknown = unknown


def integration(con):
    return con.execute("SELECT * FROM image_worker_integrations WHERE id=1").fetchone()


def _stored(con=None):
    if con is not None:
        return integration(con)
    with closing(db.connect()) as connection:
        return integration(connection)


def validate_urls(base: str, callback: str, input_base: str, allow_http: bool) -> tuple[str, str, str]:
    base, callback, input_base = (value.strip().rstrip("/") for value in (base, callback, input_base))
    if not base or not callback:
        raise ImageTaskError("image_worker_url_invalid")
    try:
        for value in (base, callback):
            parsed = urlsplit(value)
            if ((parsed.scheme != "https" and not (allow_http and parsed.scheme == "http"))
                    or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ImageTaskError("image_worker_url_invalid")
        if urlsplit(base).path not in {"", "/"}:
            raise ImageTaskError("image_worker_url_invalid")
        if input_base:
            parsed = urlsplit(input_base)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
                raise ImageTaskError("image_worker_url_invalid")
    except ValueError as exc:
        raise ImageTaskError("image_worker_url_invalid") from exc
    return base, callback, input_base


def configuration(con=None):
    row = _stored(con)
    if not row:
        raise ImageTaskError("image_worker_not_configured")
    try:
        key = security.cipher().decrypt(row["api_key_secret"].encode()).decode()
        secret = security.cipher().decrypt(row["webhook_secret"].encode()).decode()
    except Exception as exc:  # noqa: BLE001 -- never expose encrypted contents
        raise ImageTaskError("image_worker_credentials_unavailable") from exc
    base, callback, _ = validate_urls(row["base_url"], row["webhook_url"],
                                       row["input_base_url"], bool(row["allow_http"]))
    if not key or not secret:
        raise ImageTaskError("image_worker_not_configured")
    return base, key, secret, callback


def configured(con=None) -> bool:
    try:
        configuration(con)
        return True
    except ImageTaskError:
        return False


def input_base_url(con=None) -> str:
    row = _stored(con)
    if not row:
        return ""
    return validate_urls(row["base_url"], row["webhook_url"],
                         row["input_base_url"], bool(row["allow_http"]))[2]


def vendor_token() -> str:
    try:
        _, key, _, _ = configuration()
        return hmac.new(key.encode(), b"folio-nanobanana-callback", hashlib.sha256).hexdigest()
    except ImageTaskError:
        return ""


def probe(*, client=None) -> tuple[bool, str | None]:
    with closing(db.connect()) as con:
        row = integration(con)
    if not row or not row["checked_at"]:
        return False, "image_worker_no_recent_poll"
    recent = datetime.fromisoformat(row["checked_at"]) >= datetime.now(timezone.utc) - timedelta(minutes=10)
    return (True, None) if recent else (False, "image_worker_no_recent_poll")


def dimensions_from_article(article: str) -> tuple[int, int]:
    """Only a terminal _WIDTHxHEIGHT suffix defines size; legacy articles are 40×60."""
    suffix = str(article or "").rsplit("_", 1)
    if len(suffix) == 1:
        return 40, 60
    match = re.fullmatch(r"(\d{1,3})[xXхХ×](\d{1,3})", suffix[1])
    if match:
        width, height = int(match[1]), int(match[2])
        if 1 <= width <= 300 and 1 <= height <= 300:
            return width, height
        raise ImageTaskError("image_worker_dimensions_invalid")
    if re.match(r"\d+[xXхХ×]", suffix[1]):
        raise ImageTaskError("image_worker_dimensions_invalid")
    return 40, 60


def enqueue(con, item_id: int, media_id: str | list[str], prompt: str,
            width_cm: float, height_cm: float, actor: str,
            *, instance_id: int | None = None, node_id: str | None = None,
            source_preview: bool = False, chat_id: int | None = None) -> int:
    if not configured(con):
        raise ImageTaskError("image_worker_not_configured")
    if not 1 <= len(prompt.strip()) <= 4000:
        raise ImageTaskError("image_worker_prompt_invalid")
    if not (math.isfinite(width_cm) and math.isfinite(height_cm)
            and 1 <= width_cm <= 300 and 1 <= height_cm <= 300):
        raise ImageTaskError("image_worker_dimensions_invalid")
    if round(width_cm / 2.54 * 150) * round(height_cm / 2.54 * 150) > 100_000_000:
        raise ImageTaskError("image_worker_dimensions_invalid")
    media_ids = [media_id] if isinstance(media_id, str) else list(media_id)
    if not 1 <= len(media_ids) <= 8 or len(set(media_ids)) != len(media_ids):
        raise ImageTaskError("image_worker_media_missing")
    instance = con.execute("SELECT fields FROM instances WHERE item_id=?", (item_id,)).fetchone()
    collected = json.loads(instance["fields"]).get("photos", []) if instance else []
    for selected_id in media_ids:
        media = con.execute("SELECT id,mime,chat_id FROM media WHERE id=? AND item_id=?", (selected_id, item_id)).fetchone()
        approved_preview = source_preview and con.execute(
            "SELECT 1 FROM image_jobs WHERE item_id=? AND preview_media_id=? "
            "AND state='completed'", (item_id, selected_id),
        ).fetchone()
        if (not media or media["mime"] not in {"image/png", "image/jpeg", "image/webp"}
                or (source_preview and (chat_id is None or media["chat_id"] != chat_id))
                or not (db.DATA / "media" / selected_id).is_file()
                or (selected_id not in collected and not approved_preview)):
            raise ImageTaskError("image_worker_media_missing")
    stamp = db.now()
    task_id = con.execute(
        "INSERT INTO image_jobs(item_id,media_id,media_ids,input_token,prompt,width_cm,height_cm,created_at,updated_at,instance_id,node_id,chat_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, media_ids[0], json.dumps(media_ids), secrets.token_urlsafe(32), prompt.strip(), width_cm, height_cm,
         stamp, stamp, instance_id, node_id, chat_id),
    ).lastrowid
    db.audit(con, actor, "image_job.queued", "image_job", task_id)
    return task_id


def _request(method: str, path: str, *, files=None, data=None, client=None):
    base, key, _, _ = configuration()
    owned = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        return client.request(method, base + path, headers={"Authorization": f"Bearer {key}"},
                              files=files, data=data)
    finally:
        if owned:
            client.close()


def _validate_job(payload, worker_id: str):
    if not isinstance(payload, dict) or payload.get("id") != worker_id:
        raise ImageTaskError("image_worker_response_invalid", unknown=True)
    state = payload.get("status")
    if state not in PROGRESS | TERMINAL:
        raise ImageTaskError("image_worker_response_invalid", unknown=True)
    if state == "completed" and not (isinstance(payload.get("print_file"), str) and payload["print_file"].strip()):
        raise ImageTaskError("image_worker_response_invalid", unknown=True)
    return state


def _rejection_reason(response) -> str:
    code = f"image_worker_http_{response.status_code}"
    if response.status_code != 422:
        return code
    try:
        detail = response.json().get("detail")
    except (ValueError, TypeError, AttributeError):
        return code
    if isinstance(detail, str):
        return f"{code}: {detail[:200]}"
    if isinstance(detail, list):
        fields = [".".join(map(str, issue.get("loc", []))) for issue in detail[:5]
                  if isinstance(issue, dict)]
        if fields:
            return f"{code}: поля {', '.join(fields)[:200]}"
    return code


def submit_one(*, client=None) -> bool:
    if not configured():
        return False
    with db.transaction() as con:
        # This worker executes one job at a time and its v1 API has no capacity
        # endpoint. Keep exactly one Folio submission in flight. An unknown
        # outcome must hold the slot until it is reconciled, not be retried.
        if con.execute(
            "SELECT 1 FROM image_jobs WHERE state NOT IN ('pending','completed','failed') LIMIT 1"
        ).fetchone():
            return False
        row = con.execute(
            "SELECT j.*,i.external_status FROM image_jobs j JOIN items i ON i.id=j.item_id "
            "WHERE j.state='pending' ORDER BY j.id LIMIT 1"
        ).fetchone()
        if not row:
            return False
        if row["external_status"] == "cancelled":
            con.execute("UPDATE image_jobs SET state='failed',error=?,updated_at=? WHERE id=?",
                        ("image_worker_order_cancelled", db.now(), row["id"]))
            return True
        con.execute("UPDATE image_jobs SET state='submitting',updated_at=? WHERE id=?", (db.now(), row["id"]))
    state, error, worker_id, payload = "unknown", "image_worker_submission_unknown", None, None
    try:
        _, _, _, callback = configuration()
        media_ids = json.loads(row["media_ids"]) or [row["media_id"]]
        # Media files have opaque IDs; take the verified MIME from the database.
        with db.transaction() as con:
            media_rows = [con.execute("SELECT mime FROM media WHERE id=? AND item_id=?", (media_id, row["item_id"])).fetchone()
                          for media_id in media_ids]
        if any(not media or not (db.DATA / "media" / media_id).is_file()
               for media, media_id in zip(media_rows, media_ids)):
            raise ImageTaskError("image_worker_media_missing")
        data = {"prompt": row["prompt"], "width_cm": str(row["width_cm"]),
                "height_cm": str(row["height_cm"]), "webhook_url": callback}
        public_base = input_base_url()
        if public_base:
            prefix = os.environ.get("MODULE_PREFIX", "/folio").rstrip("/")
            data["image_url"] = [f"{public_base}{prefix}/image-worker/input/{quote(row['input_token'], safe='')}/{index}"
                                 for index in range(len(media_ids))]
            response = _request("POST", "/v1/jobs", client=client, data=data)
        else:
            with ExitStack() as stack:
                files = [("image", ("source", stack.enter_context((db.DATA / "media" / media_id).open("rb")), media["mime"]))
                         for media_id, media in zip(media_ids, media_rows)]
                response = _request("POST", "/v1/jobs", client=client,
                                    files=files, data=data)
        if response.status_code != 202:
            raise ImageTaskError(_rejection_reason(response),
                                 unknown=response.status_code >= 500)
        payload = response.json()
        worker_id = str(uuid.UUID(str(payload.get("id"))))
        state = _validate_job(payload, worker_id)
        error = None
    except ImageTaskError as exc:
        state, error = ("unknown" if exc.unknown else "failed"), exc.code
    except (httpx.TimeoutException, httpx.TransportError, ValueError, KeyError, TypeError):
        state, error = "unknown", "image_worker_submission_unknown"
    except OSError:
        state, error = "failed", "image_worker_media_missing"
    except Exception:  # noqa: BLE001 -- never expose external response bodies
        state, error = "unknown", "image_worker_submission_unknown"
    with db.transaction() as con:
        print_file = str(payload.get("print_file") or "")[:1000] if isinstance(payload, dict) and state == "completed" else None
        if isinstance(payload, dict) and state == "failed":
            error = str(payload.get("error") or "image_worker_failed")[:500]
        con.execute(
            "UPDATE image_jobs SET state=?,worker_id=?,error=?,print_file=?,updated_at=?,next_poll_at=? WHERE id=?",
            (state, worker_id, error, print_file, db.now(),
             _next_poll() if worker_id and state in PROGRESS | {"unknown"} else None, row["id"]),
        )
        db.audit(con, "system", f"image_job.{state}", "image_job", row["id"])
        if state in TERMINAL:
            _resume_scenario(con, row)
    return True


def _resume_scenario(con, row):
    if row["instance_id"] is not None:
        from . import scenarios  # avoid the scenario-engine/adapter import cycle
        scenarios.advance(con, row["instance_id"])


def _next_poll() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=POLL_SECONDS)).isoformat()


def poll_one(*, client=None) -> bool:
    if not configured():
        return False
    with db.transaction() as con:
        row = con.execute(
            "SELECT * FROM image_jobs WHERE state IN ('queued','generating','waiting_topaz','composing','webhook_pending','unknown') "
            "AND worker_id IS NOT NULL AND next_poll_at<=? ORDER BY next_poll_at,id LIMIT 1", (db.now(),),
        ).fetchone()
        if not row:
            return False
        con.execute("UPDATE image_jobs SET next_poll_at=? WHERE id=?", (_next_poll(), row["id"]))
    try:
        response = _request("GET", f"/v1/jobs/{row['worker_id']}", client=client)
        if response.status_code != 200:
            raise ImageTaskError(f"image_worker_http_{response.status_code}")
        payload = response.json()
        state = _validate_job(payload, row["worker_id"])
    except (ImageTaskError, httpx.HTTPError, ValueError, TypeError) as exc:
        # Keep the known worker ID and retry the read later. Never resubmit.
        code = exc.code if isinstance(exc, ImageTaskError) else "image_worker_status_unavailable"
        with db.transaction() as con:
            con.execute("UPDATE image_jobs SET state='unknown',error=?,updated_at=? WHERE id=?",
                        (code, db.now(), row["id"]))
        return True
    apply_result(row["worker_id"], payload, state)
    return True


def apply_result(worker_id: str, payload: dict, state: str) -> bool:
    if state not in PROGRESS | TERMINAL:
        raise ImageTaskError("image_worker_response_invalid")
    if state == "completed" and not (isinstance(payload.get("print_file"), str) and payload["print_file"].strip()):
        raise ImageTaskError("image_worker_response_invalid")
    with db.transaction() as con:
        row = con.execute("SELECT * FROM image_jobs WHERE worker_id=?", (worker_id,)).fetchone()
        if not row:
            return False
        if row["state"] in TERMINAL:
            return True
        error = str(payload.get("error") or "")[:500] if state == "failed" else None
        print_file = str(payload.get("print_file") or "")[:1000] if state == "completed" else None
        con.execute(
            "UPDATE image_jobs SET state=?,error=?,print_file=?,updated_at=?,next_poll_at=? WHERE id=?",
            (state, error, print_file, db.now(), _next_poll() if state in PROGRESS else None, row["id"]),
        )
        if state in TERMINAL:
            db.audit(con, "system", f"image_job.{state}", "image_job", row["id"])
            _resume_scenario(con, row)
    return True


def accept_webhook(raw: bytes, signature: str, event: str, delivery_id: str) -> bool:
    _, _, secret, _ = configuration()
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ImageTaskError("image_worker_signature_invalid")
    if len(raw) > 16_384:
        raise ImageTaskError("image_worker_response_invalid")
    try:
        payload = json.loads(raw)
        worker_id = str(uuid.UUID(str(payload["id"])))
    except (ValueError, TypeError, KeyError) as exc:
        raise ImageTaskError("image_worker_response_invalid") from exc
    state = _validate_job(payload, worker_id)
    if state not in TERMINAL or event != f"job.{state}" or delivery_id != worker_id:
        raise ImageTaskError("image_worker_response_invalid")
    return apply_result(worker_id, payload, state)


def preview(worker_id: str, *, client=None) -> bytes:
    response = _request("GET", f"/v1/jobs/{worker_id}/preview", client=client)
    if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/jpeg"):
        raise ImageTaskError("image_worker_preview_unavailable")
    if len(response.content) > 10 * 1024 * 1024:
        raise ImageTaskError("image_worker_preview_unavailable")
    return response.content


def authenticate_pull(con, authorization: str) -> None:
    row = integration(con)
    if not row:
        raise ImageTaskError("image_worker_not_configured")
    try:
        key = security.cipher().decrypt(row["api_key_secret"].encode()).decode()
    except Exception as exc:
        raise ImageTaskError("image_worker_credentials_unavailable") from exc
    if not key or not hmac.compare_digest(authorization, f"Bearer {key}"):
        raise ImageTaskError("image_worker_auth_failed")


def claim_next(con) -> dict | None:
    """Return the same claimed job until its terminal result is acknowledged."""
    row = con.execute(
        "SELECT j.*,i.external_status FROM image_jobs j JOIN items i ON i.id=j.item_id "
        "WHERE j.pull_claimed=1 AND j.state NOT IN ('completed','failed') ORDER BY j.id LIMIT 1"
    ).fetchone()
    if not row:
        row = con.execute(
            "SELECT j.*,i.external_status FROM image_jobs j JOIN items i ON i.id=j.item_id "
            "WHERE j.pull_claimed=0 AND j.worker_id IS NOT NULL "
            "AND j.state NOT IN ('completed','failed') ORDER BY j.id LIMIT 1"
        ).fetchone()
    if not row:
        row = con.execute(
            "SELECT j.*,i.external_status FROM image_jobs j JOIN items i ON i.id=j.item_id "
            "WHERE j.state='pending' ORDER BY j.id LIMIT 1"
        ).fetchone()
    if not row:
        return None
    if row["external_status"] == "cancelled":
        con.execute("UPDATE image_jobs SET state='failed',error=?,updated_at=? WHERE id=?",
                    ("image_worker_order_cancelled", db.now(), row["id"]))
        return None
    public_base = os.environ.get("FOLIO_PUBLIC_BASE_URL", "").rstrip("/")
    if urlsplit(public_base).scheme != "https":
        raise ImageTaskError("image_worker_url_invalid")
    worker_id = row["worker_id"] or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{public_base}/folio/image-jobs/{row['id']}"))
    if not row["pull_claimed"]:
        con.execute("UPDATE image_jobs SET state=?,pull_claimed=1,worker_id=?,updated_at=? WHERE id=?",
                    ("queued" if row["state"] == "pending" else row["state"], worker_id, db.now(), row["id"]))
    media_ids = json.loads(row["media_ids"]) or [row["media_id"]]
    prefix = os.environ.get("MODULE_PREFIX", "/folio").rstrip("/")
    return {"id": row["id"], "worker_id": worker_id, "prompt": row["prompt"],
            "width_cm": row["width_cm"], "height_cm": row["height_cm"],
            "image_urls": [f"{public_base}{prefix}/image-worker/input/{quote(row['input_token'], safe='')}/{index}"
                           for index in range(len(media_ids))]}


def receive_update(con, job_id: int, worker_id: str, state: str,
                   print_file: str = "", error: str = "", preview_bytes: bytes | None = None) -> None:
    row = con.execute("SELECT j.*,COALESCE(j.chat_id,inst.chat_id) AS result_chat_id "
                      "FROM image_jobs j "
                      "LEFT JOIN instances inst ON inst.id=j.instance_id WHERE j.id=?", (job_id,)).fetchone()
    if not row or not row["pull_claimed"] or row["worker_id"] != worker_id:
        raise ImageTaskError("image_worker_job_unknown")
    if row["state"] in TERMINAL:
        if row["state"] != state:
            raise ImageTaskError("image_worker_result_conflict")
        return
    ranks = {"queued": 0, "generating": 1, "waiting_topaz": 2, "composing": 3,
             "completed": 4, "failed": 4}
    if state not in ranks:
        raise ImageTaskError("image_worker_response_invalid")
    if ranks[state] < ranks.get(row["state"], 0):
        return
    preview_media_id = None
    if state == "completed":
        if not print_file.strip() or len(print_file) > 1000 or not preview_bytes:
            raise ImageTaskError("image_worker_result_incomplete")
        if len(preview_bytes) > 10 * 1024 * 1024:
            raise ImageTaskError("image_worker_preview_invalid")
        try:
            with Image.open(io.BytesIO(preview_bytes)) as image:
                if image.format != "JPEG" or max(image.size) != 1000:
                    raise ImageTaskError("image_worker_preview_invalid")
        except OSError as exc:
            raise ImageTaskError("image_worker_preview_invalid") from exc
        from . import media, scenarios
        try:
            preview_media_id = media.store(con, preview_bytes, db.config(con), row["result_chat_id"], row["item_id"])
        except scenarios.Invalid as exc:
            raise ImageTaskError("image_worker_preview_invalid") from exc
    con.execute("UPDATE image_jobs SET state=?,error=?,print_file=?,preview_media_id=COALESCE(?,preview_media_id),updated_at=? WHERE id=?",
                (state, error[:500] if state == "failed" else None,
                 print_file if state == "completed" else None, preview_media_id, db.now(), job_id))
    if (state == "completed" and row["instance_id"] is None
            and str(row["node_id"] or "").startswith("rework:")):
        _queue_rework_result(con, row, preview_media_id)
    if state in TERMINAL:
        db.audit(con, "system", f"image_job.{state}", "image_job", job_id, row["result_chat_id"])
        _resume_scenario(con, row)


def _queue_rework_result(con, job, preview_media_id):
    """Queue one Ozon file send only for the original active manual chat."""
    chat = con.execute(
        "SELECT c.*,i.external_status FROM chats c JOIN items i ON i.id=? "
        "WHERE c.id=? AND c.account_id=i.account_id",
        (job["item_id"], job["chat_id"]),
    ).fetchone()
    reason = None
    if not chat or chat["active_item"] != job["item_id"]:
        reason = "rework_chat_changed"
    elif chat["external_status"] == "cancelled":
        reason = "rework_order_cancelled"
    elif chat["mode"] != "manual":
        reason = "rework_bot_resumed"
    if reason:
        con.execute("UPDATE image_jobs SET delivery_error=? WHERE id=?", (reason, job["id"]))
        db.audit(con, "system", "image_job.delivery_skipped", "image_job", job["id"], job["chat_id"])
        return
    dedup = f"rework-result:{job['id']}"
    if con.execute("SELECT 1 FROM outbox WHERE dedup=?", (dedup,)).fetchone():
        return
    outbox_id = con.execute(
        "INSERT INTO outbox(chat_id,instance_id,actor,body,dedup,epoch,created_at,kind,media_id) "
        "VALUES(?,NULL,'system','Доработанный макет',?,?,?,'file',?)",
        (chat["id"], dedup, chat["epoch"], db.now(), preview_media_id),
    ).lastrowid
    db.audit(con, "system", "image_job.delivery_queued", "outbox", outbox_id, chat["id"])
