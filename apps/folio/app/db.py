"""Folio's private database. All state changes use an explicit write transaction."""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(os.environ.get("FOLIO_DATA_DIR", "data/folio"))


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','manager')));
CREATE TABLE IF NOT EXISTS revoked_users(user_id TEXT PRIMARY KEY REFERENCES users(id));
CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
INSERT OR IGNORE INTO settings VALUES(1,'{}');
CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY, name TEXT NOT NULL, client_id TEXT NOT NULL, secret TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '{}', checked_at TEXT, error TEXT, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS sync_state(account_id INTEGER PRIMARY KEY REFERENCES accounts(id), orders_cursor TEXT, failures INTEGER NOT NULL DEFAULT 0, blocked_until TEXT, last_success TEXT);
CREATE TABLE IF NOT EXISTS scenarios(id INTEGER PRIMARY KEY, name TEXT NOT NULL, product_type TEXT NOT NULL CHECK(product_type IN ('portrait_background','collage','template_art')), draft TEXT NOT NULL, published INTEGER, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS versions(id INTEGER PRIMARY KEY, scenario_id INTEGER NOT NULL REFERENCES scenarios(id), number INTEGER NOT NULL, graph TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(scenario_id,number));
CREATE TABLE IF NOT EXISTS templates(id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, media_id TEXT);
CREATE TABLE IF NOT EXISTS mappings(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), offer_id TEXT NOT NULL, product_type TEXT NOT NULL, scenario_id INTEGER NOT NULL REFERENCES scenarios(id), template_ids TEXT NOT NULL DEFAULT '[]', UNIQUE(account_id,offer_id));
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), posting TEXT NOT NULL, sku TEXT NOT NULL, offer_id TEXT NOT NULL, name TEXT NOT NULL, quantity INTEGER NOT NULL, external_status TEXT NOT NULL, mapping_id INTEGER REFERENCES mappings(id), UNIQUE(account_id,posting,sku));
CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), external_id TEXT NOT NULL, title TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'manual', epoch INTEGER NOT NULL DEFAULT 0, active_item INTEGER REFERENCES items(id), unread INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, UNIQUE(account_id,external_id));
CREATE TABLE IF NOT EXISTS chat_items(chat_id INTEGER NOT NULL REFERENCES chats(id), item_id INTEGER NOT NULL REFERENCES items(id), PRIMARY KEY(chat_id,item_id));
CREATE TABLE IF NOT EXISTS assignments(chat_id INTEGER NOT NULL REFERENCES chats(id), user_id TEXT NOT NULL REFERENCES users(id), PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS instances(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL UNIQUE REFERENCES items(id), chat_id INTEGER NOT NULL REFERENCES chats(id), version_id INTEGER NOT NULL REFERENCES versions(id), node TEXT NOT NULL, fields TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'collecting', revision INTEGER NOT NULL DEFAULT 0, prompted INTEGER NOT NULL DEFAULT 0, confirmed_at TEXT, reviewed_at TEXT, reviewed_by TEXT, ready_at TEXT, template_ids TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id), external_id TEXT, actor TEXT NOT NULL, body TEXT NOT NULL, media_ids TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, UNIQUE(chat_id,external_id));
CREATE TABLE IF NOT EXISTS media(id TEXT PRIMARY KEY, chat_id INTEGER REFERENCES chats(id), item_id INTEGER REFERENCES items(id), mime TEXT NOT NULL, size INTEGER NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), external_key TEXT NOT NULL, chat_id INTEGER NOT NULL REFERENCES chats(id), body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error TEXT, UNIQUE(account_id,external_key));
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id), instance_id INTEGER REFERENCES instances(id), actor TEXT NOT NULL, body TEXT NOT NULL, dedup TEXT NOT NULL UNIQUE, epoch INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sent','failed','unknown','cancelled')), external_id TEXT, error TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, account_id INTEGER REFERENCES accounts(id), payload TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'pending', error TEXT, created_at TEXT NOT NULL, finished_at TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT NOT NULL, chat_id INTEGER, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pending_outbox ON outbox(state,id);
CREATE INDEX IF NOT EXISTS pending_jobs ON jobs(state,id);
"""


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA / "folio.db", timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def initialize():
    with closing(connect()) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        admin = os.environ.get("FOLIO_ADMIN_USER_ID")
        if admin:
            con.execute(
                "INSERT OR IGNORE INTO users VALUES(?,?,'admin')",
                (admin, "Администратор Folio"),
            )
        con.commit()


@contextmanager
def transaction():
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def audit(con, actor, action, kind, object_id, chat_id=None):
    # Deliberately no payload, credentials, message text or customer photo URLs.
    con.execute(
        "INSERT INTO audit(actor,action,object_type,object_id,chat_id,created_at) VALUES(?,?,?,?,?,?)",
        (str(actor), action, kind, str(object_id), chat_id, now()),
    )


def config(con):
    return json.loads(
        con.execute("SELECT value FROM settings WHERE id=1").fetchone()[0]
    )


def job(con, kind, account_id=None, payload=None):
    if con.execute(
        "SELECT 1 FROM jobs WHERE kind=? AND account_id IS ? AND state IN ('pending','running')",
        (kind, account_id),
    ).fetchone():
        return
    con.execute(
        "INSERT INTO jobs(kind,account_id,payload,created_at) VALUES(?,?,?,?)",
        (kind, account_id, dump(payload or {}), now()),
    )
