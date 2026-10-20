"""Placement phase of image_jobs. Core stores metadata; only the owner writes files."""
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from . import db

LEASE_SECONDS = 600
RETRY_SECONDS = 60


def identity(value):
    return str(uuid.UUID(value))


def validate_files(value, quantity):
    if not isinstance(value, list) or len(value) != quantity or not value:
        raise ValueError("MES должна вернуть print_files для каждого экземпляра")
    serials, names, codes = set(), set(), set()
    for entry in value:
        if not isinstance(entry, dict) or type(entry.get("serial")) is not int or entry["serial"] <= 0:
            raise ValueError("Некорректный serial в print_files")
        for field in ("instance_code", "layout_key", "filename"):
            text = entry.get(field)
            if not isinstance(text, str) or not text.strip() or len(text) > 255 or any(ord(c) < 32 for c in text):
                raise ValueError("Некорректное поле " + field + " в print_files")
        name = entry["filename"]
        if (re.search(r'[<>:"/\\|?*]', name) or name.endswith((" ", "."))
                or name in {".", ".."}
                or re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name)):
            raise ValueError("Небезопасное имя файла от MES")
        if "." not in name or name.rsplit(".", 1)[0] != entry["layout_key"]:
            raise ValueError("layout_key не соответствует filename")
        if entry["serial"] in serials or name.casefold() in names or entry["instance_code"] in codes:
            raise ValueError("MES вернула повторяющиеся экземпляры")
        serials.add(entry["serial"])
        names.add(name.casefold())
        codes.add(entry["instance_code"])
    if len(db.dump(value).encode()) > 65536:
        raise ValueError("Слишком большой print_files")
    return value


def schedule(con, action, files):
    job = con.execute("SELECT * FROM image_jobs WHERE id=?", (action["image_job_id"],)).fetchone()
    if not job or job["state"] != "completed" or not job["preview_media_id"] or not job["owner_id"]:
        return "Неизвестен воркер-владелец исходного макета; размещение не назначено"
    if job["placement_id"] and job["placement_id"] != action["id"]:
        return "Для макета уже существует другая операция размещения"
    if job["placement_files"] and json.loads(job["placement_files"]) != files:
        return "MES изменила имена существующей операции; требуется сверка"
    con.execute(
        "UPDATE image_jobs SET placement_id=?,placement_version=?,placement_files=?,"
        "placement_state=COALESCE(placement_state,'pending') WHERE id=?",
        (action["id"], job["preview_media_id"], db.dump(files), job["id"]),
    )
    return None


def approved(con, instance_id, job):
    instance = con.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
    if not instance or not job or instance["approved_image_job_id"] != job["id"]:
        return False
    last = con.execute("SELECT o.media_id FROM outbox o JOIN media m ON m.id=o.media_id AND m.item_id=? "
                       "WHERE o.chat_id=? AND o.kind='file' AND o.state='sent' ORDER BY o.id DESC LIMIT 1",
                       (job["item_id"], instance["chat_id"])).fetchone()
    return bool(last and last[0] == job["preview_media_id"])


def current(con, job):
    action = con.execute("SELECT * FROM order_webhook_actions WHERE id=?", (job["placement_id"],)).fetchone()
    instance = con.execute("SELECT * FROM instances WHERE id=?", (action["instance_id"],)).fetchone() if action else None
    item = con.execute("SELECT external_status FROM items WHERE id=?", (job["item_id"],)).fetchone()
    return bool(action and action["state"] == "sent" and not action["placement_error"] and instance
                and approved(con, instance["id"], job)
                and job["placement_version"] == job["preview_media_id"]
                and item["external_status"] != "cancelled")


def claim(con, owner, session):
    stamp = db.now()
    rows = con.execute(
        "SELECT * FROM image_jobs WHERE owner_id=? AND placement_state IN ('pending','running','partial') "
        "AND (placement_due IS NULL OR placement_due<=?) ORDER BY id", (owner, stamp),
    ).fetchall()
    for job in rows:
        if not current(con, job):
            con.execute("UPDATE image_jobs SET placement_state='stale' WHERE id=?", (job["id"],))
            continue
        active = job["placement_state"] == "running" and job["placement_until"] and job["placement_until"] > stamp
        if active and job["placement_session"] != session:
            continue
        token = job["placement_token"] if active else str(uuid.uuid4())
        until = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
        con.execute("UPDATE image_jobs SET placement_state='running',placement_session=?,placement_token=?,"
                    "placement_until=? WHERE id=?", (session, token, until, job["id"]))
        results = json.loads(job["placement_results"])
        action = con.execute("SELECT external_order_id FROM order_webhook_actions WHERE id=?",
                             (job["placement_id"],)).fetchone()
        return {
            "action": "place_print_files", "id": job["id"], "worker_id": job["worker_id"],
            "worker_instance_id": owner, "item_id": job["item_id"],
            "layout_version": job["placement_version"], "operation_id": job["placement_id"],
            "claim_token": token, "lease_until": until, "external_order_id": action["external_order_id"],
            "source_print_file": job["print_file"], "destination": {"kind": "worker_print_directory"},
            "print_files": json.loads(job["placement_files"]), "results": results,
            "pending_serials": [f["serial"] for f in json.loads(job["placement_files"])
                                if results.get(str(f["serial"]), {}).get("status") != "completed"],
        }
    return None


def report(con, job_id, owner, session, payload):
    job = con.execute("SELECT * FROM image_jobs WHERE id=?", (job_id,)).fetchone()
    if (not job or job["owner_id"] != owner or job["placement_session"] != session
            or payload.get("operation_id") != job["placement_id"]
            or payload.get("layout_version") != job["placement_version"]
            or payload.get("claim_token") != job["placement_token"] or not current(con, job)):
        raise ValueError("Устаревший отчёт или другой владелец операции")
    if job["placement_state"] != "completed" and (not job["placement_until"] or job["placement_until"] <= db.now()):
        raise ValueError("Захват операции истёк; повторите опрос")
    updates = payload.get("files", [])
    if not isinstance(updates, list) or len(updates) > len(json.loads(job["placement_files"])):
        raise ValueError("Некорректный список результатов")
    expected = {f["serial"]: f for f in json.loads(job["placement_files"])}
    results, seen = json.loads(job["placement_results"]), set()
    for update in updates:
        if not isinstance(update, dict):
            raise ValueError("Некорректный результат файла")
        serial = update.get("serial")
        if type(serial) is not int or serial not in expected or serial in seen:
            raise ValueError("Неизвестный или повторяющийся serial")
        seen.add(serial)
        if update.get("filename") != expected[serial]["filename"] or update.get("status") not in {"completed", "failed"}:
            raise ValueError("Неверное имя или статус файла")
        result = {k: update[k] for k in ("serial", "filename", "status")}
        if update["status"] == "completed":
            path = update.get("saved_path")
            if not isinstance(path, str) or not path.strip() or len(path) > 2000 or any(ord(c) < 32 for c in path):
                raise ValueError("Укажите фактический путь сохранения")
            if path.replace("\\", "/").rsplit("/", 1)[-1] != expected[serial]["filename"]:
                raise ValueError("Имя сохранённого файла отличается от MES")
            result["saved_path"] = path
            if "size_bytes" in update:
                if type(update["size_bytes"]) is not int or update["size_bytes"] <= 0:
                    raise ValueError("Некорректный размер файла")
                result["size_bytes"] = update["size_bytes"]
            if "sha256" in update:
                if not isinstance(update["sha256"], str) or not re.fullmatch("[a-fA-F0-9]{64}", update["sha256"]):
                    raise ValueError("Некорректная контрольная сумма")
                result["sha256"] = update["sha256"].lower()
        else:
            if not isinstance(update.get("error"), str) or not update["error"].strip():
                raise ValueError("Укажите ошибку файла")
            result["error"] = update["error"][:1000]
        old = results.get(str(serial))
        if old and old["status"] == "completed":
            if old != result:
                raise ValueError("Успешный результат нельзя заменить другим отчётом")
        else:
            results[str(serial)] = result
    status = payload.get("status")
    if status not in {"running", "finished"}:
        raise ValueError("Статус отчёта должен быть running или finished")
    if job["placement_state"] == "partial" and status == "running":
        raise ValueError("Попытка завершена; получите новый захват")
    completed = all(results.get(str(s), {}).get("status") == "completed" for s in expected)
    state = "completed" if completed else "partial" if status == "finished" else "running"
    until = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    due = (datetime.now(timezone.utc) + timedelta(seconds=RETRY_SECONDS)).isoformat() if state == "partial" else None
    con.execute("UPDATE image_jobs SET placement_results=?,placement_state=?,placement_until=?,placement_due=? WHERE id=?",
                (db.dump(results), state, until, due, job_id))
    db.audit(con, "system", "image_job.placement_" + state, "image_job", job_id)
    return {"ok": True, "placement_state": state, "results": results}
