import hmac
import os
from urllib.parse import unquote

from cryptography.fernet import Fernet
from fastapi import HTTPException

from . import db


def cipher():
    key = os.environ.get("FOLIO_ENCRYPTION_KEY")
    if not key:
        db.DATA.mkdir(parents=True, exist_ok=True)
        path = db.DATA / "credentials.key"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as output:
                output.write(Fernet.generate_key())
        key = path.read_bytes()
    return Fernet(key)


def user(request, con, admin=False):
    uid = request.headers.get("X-Bellenne-User-Id", "")
    if not uid:
        raise HTTPException(401, "Войдите через Bellenne")
    record = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if (
        not record
        or con.execute("SELECT 1 FROM revoked_users WHERE user_id=?", (uid,)).fetchone()
        or (admin and record["role"] != "admin")
    ):
        raise HTTPException(403, "Нет доступа к этому разделу Folio")
    return record


def csrf(request, supplied):
    expected = unquote(request.headers.get("X-Bellenne-Csrf-Token", ""))
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(403, "Обновите страницу: неверный CSRF-токен")


def chat_access(con, actor, chat_id):
    chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    if not chat:
        raise HTTPException(404, "Чат не найден")
    if actor["role"] == "admin":
        return chat
    if db.config(con).get("manager_scope") == "all":
        return chat
    if not con.execute(
        "SELECT 1 FROM assignments WHERE chat_id=? AND user_id=?",
        (chat_id, actor["id"]),
    ).fetchone():
        raise HTTPException(403, "Чат не назначен этому менеджеру")
    return chat
