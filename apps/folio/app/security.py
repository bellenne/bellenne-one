import hmac
import binascii
import hashlib
import json
import os
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from urllib.parse import unquote

from cryptography.fernet import Fernet
from fastapi import HTTPException

from . import db

COOKIE_NAME = "folio_session"
SESSION_SECONDS = 12 * 60 * 60


def _secret():
    db.DATA.mkdir(parents=True, exist_ok=True)
    path = db.DATA / "sessions.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as output:
            output.write(secrets.token_bytes(32))
    return path.read_bytes()


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"{salt.hex()}:{digest.hex()}"


def check_password(password, stored):
    try:
        salt_hex, expected = stored.split(":", 1)
        actual = password_hash(password, bytes.fromhex(salt_hex)).split(":", 1)[1]
        return hmac.compare_digest(actual, expected)
    except (AttributeError, ValueError):
        return False


def issue_session(record, con):
    payload = {"id": record["id"], "exp": int(time.time()) + SESSION_SECONDS,
               "csrf": secrets.token_urlsafe(24),
               "password": hashlib.sha256(record["password_hash"].encode()).hexdigest()}
    encoded = urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    signature = hmac.new(_secret(), encoded.encode(), hashlib.sha256).hexdigest()
    token = f"{encoded}.{signature}"
    con.execute("DELETE FROM local_sessions WHERE expires_at<=?", (int(time.time()),))
    con.execute("INSERT INTO local_sessions(id,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), record["id"], payload["exp"], db.now()))
    return token


def revoke_session(token, con):
    if token:
        con.execute("DELETE FROM local_sessions WHERE id=?",
                    (hashlib.sha256(token.encode()).hexdigest(),))


def session_user(token, con):
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(_secret(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if payload["exp"] <= int(time.time()):
            return None
        if not con.execute("SELECT 1 FROM local_sessions WHERE id=? AND user_id=? AND expires_at>?",
                           (hashlib.sha256(token.encode()).hexdigest(), payload["id"], int(time.time()))).fetchone():
            return None
        record = con.execute("SELECT * FROM users WHERE id=? AND local_login IS NOT NULL",
                             (payload["id"],)).fetchone()
        if not record or record["role"] != "manager" or not record["password_hash"]:
            return None
        if con.execute("SELECT 1 FROM revoked_users WHERE user_id=?", (record["id"],)).fetchone():
            return None
        if not hmac.compare_digest(payload["password"], hashlib.sha256(record["password_hash"].encode()).hexdigest()):
            return None
        return record, payload["csrf"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, binascii.Error):
        return None


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
    if not chat or chat["start_job_id"] is None or not con.execute(
        "SELECT 1 FROM chat_items ci JOIN items i ON i.id=ci.item_id "
        "JOIN mappings m ON m.id=i.mapping_id WHERE ci.chat_id=? "
        "AND m.account_id=i.account_id AND m.key_kind='seller_article' "
        "AND m.sku=i.offer_id LIMIT 1",
        (chat_id,),
    ).fetchone():
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
