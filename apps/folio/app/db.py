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
CREATE TABLE IF NOT EXISTS local_sessions(id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
INSERT OR IGNORE INTO settings VALUES(1,'{}');
CREATE TABLE IF NOT EXISTS retailcrm_integrations(id INTEGER PRIMARY KEY CHECK(id=1), base_url TEXT NOT NULL, site TEXT NOT NULL, secret TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '{}', checked_at TEXT, error TEXT, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS mattermost_integrations(id INTEGER PRIMARY KEY CHECK(id=1), secret TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS image_worker_integrations(id INTEGER PRIMARY KEY CHECK(id=1), base_url TEXT NOT NULL, api_key_secret TEXT NOT NULL, webhook_secret TEXT NOT NULL, webhook_url TEXT NOT NULL, input_base_url TEXT NOT NULL DEFAULT '', allow_http INTEGER NOT NULL DEFAULT 0, vendor_token_secret TEXT NOT NULL DEFAULT '', checked_at TEXT, error TEXT, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY, name TEXT NOT NULL, client_id TEXT NOT NULL, secret TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '{}', checked_at TEXT, error TEXT, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS sync_state(account_id INTEGER PRIMARY KEY REFERENCES accounts(id), orders_cursor TEXT, failures INTEGER NOT NULL DEFAULT 0, blocked_until TEXT, last_success TEXT);
CREATE TABLE IF NOT EXISTS scenarios(id INTEGER PRIMARY KEY, name TEXT NOT NULL, product_type TEXT NOT NULL CHECK(product_type IN ('portrait_background','collage','template_art')), draft TEXT NOT NULL, published INTEGER, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS versions(id INTEGER PRIMARY KEY, scenario_id INTEGER NOT NULL REFERENCES scenarios(id), number INTEGER NOT NULL, graph TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(scenario_id,number));
CREATE TABLE IF NOT EXISTS templates(id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, media_id TEXT);
CREATE TABLE IF NOT EXISTS mappings(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), sku TEXT NOT NULL, product_type TEXT NOT NULL, scenario_id INTEGER NOT NULL REFERENCES scenarios(id), template_ids TEXT NOT NULL DEFAULT '[]', UNIQUE(account_id,sku));
CREATE TABLE IF NOT EXISTS ozon_products(account_id INTEGER NOT NULL REFERENCES accounts(id), offer_id TEXT NOT NULL, ozon_sku TEXT NOT NULL DEFAULT '', product_id TEXT NOT NULL DEFAULT '', PRIMARY KEY(account_id,offer_id));
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), posting TEXT NOT NULL, order_number TEXT NOT NULL DEFAULT '', sku TEXT NOT NULL, offer_id TEXT NOT NULL, name TEXT NOT NULL, customer_name TEXT NOT NULL DEFAULT '', quantity INTEGER NOT NULL, external_status TEXT NOT NULL, order_started_at TEXT, mapping_id INTEGER REFERENCES mappings(id), UNIQUE(account_id,posting,sku));
CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), external_id TEXT NOT NULL, posting TEXT, title TEXT NOT NULL, chat_type TEXT NOT NULL DEFAULT '', buyer_id TEXT NOT NULL DEFAULT '', buyer_name TEXT NOT NULL DEFAULT '', buyer_name_manual TEXT NOT NULL DEFAULT '', start_job_id INTEGER REFERENCES jobs(id), mode TEXT NOT NULL DEFAULT 'manual', epoch INTEGER NOT NULL DEFAULT 0, active_item INTEGER REFERENCES items(id), unread INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, UNIQUE(account_id,posting));
CREATE TABLE IF NOT EXISTS chat_items(chat_id INTEGER NOT NULL REFERENCES chats(id), item_id INTEGER NOT NULL REFERENCES items(id), PRIMARY KEY(chat_id,item_id));
CREATE TABLE IF NOT EXISTS order_lookup_attempts(account_id INTEGER NOT NULL REFERENCES accounts(id), order_number TEXT NOT NULL, checked_at TEXT NOT NULL, PRIMARY KEY(account_id,order_number));
CREATE TABLE IF NOT EXISTS assignments(chat_id INTEGER NOT NULL REFERENCES chats(id), user_id TEXT NOT NULL REFERENCES users(id), PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS instances(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL UNIQUE REFERENCES items(id), chat_id INTEGER NOT NULL REFERENCES chats(id), version_id INTEGER NOT NULL REFERENCES versions(id), node TEXT NOT NULL, fields TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'collecting', revision INTEGER NOT NULL DEFAULT 0, prompted INTEGER NOT NULL DEFAULT 0, confirmed_at TEXT, reviewed_at TEXT, reviewed_by TEXT, ready_at TEXT, template_ids TEXT NOT NULL DEFAULT '[]', approval_due_at TEXT);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id), external_id TEXT, actor TEXT NOT NULL, body TEXT NOT NULL, media_ids TEXT NOT NULL DEFAULT '[]', order_number TEXT NOT NULL DEFAULT '', context_sku TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, UNIQUE(chat_id,external_id));
CREATE TABLE IF NOT EXISTS media(id TEXT PRIMARY KEY, chat_id INTEGER REFERENCES chats(id), item_id INTEGER REFERENCES items(id), mime TEXT NOT NULL, size INTEGER NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), external_key TEXT NOT NULL, chat_id INTEGER NOT NULL REFERENCES chats(id), body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error TEXT, UNIQUE(account_id,external_key));
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id), instance_id INTEGER REFERENCES instances(id), actor TEXT NOT NULL, body TEXT NOT NULL, dedup TEXT NOT NULL UNIQUE, epoch INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sent','failed','unknown','cancelled')), external_id TEXT, error TEXT, created_at TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'text', media_id TEXT REFERENCES media(id));
CREATE TABLE IF NOT EXISTS retailcrm_actions(id INTEGER PRIMARY KEY, instance_id INTEGER NOT NULL REFERENCES instances(id), node_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','sent','failed','unknown')), external_id TEXT NOT NULL, retailcrm_id INTEGER, error TEXT, created_at TEXT NOT NULL, finished_at TEXT, kind TEXT NOT NULL DEFAULT 'create');
CREATE TABLE IF NOT EXISTS mattermost_actions(id INTEGER PRIMARY KEY, instance_id INTEGER NOT NULL REFERENCES instances(id), node_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','sent','failed','unknown')), error TEXT, created_at TEXT NOT NULL, finished_at TEXT, UNIQUE(instance_id,node_id));
CREATE TABLE IF NOT EXISTS image_jobs(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), media_id TEXT NOT NULL REFERENCES media(id), media_ids TEXT NOT NULL DEFAULT '[]', input_token TEXT UNIQUE, prompt TEXT NOT NULL, width_cm REAL NOT NULL, height_cm REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','submitting','unknown','queued','generating','waiting_topaz','composing','webhook_pending','completed','failed')), worker_id TEXT UNIQUE, error TEXT, print_file TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, next_poll_at TEXT, instance_id INTEGER REFERENCES instances(id), node_id TEXT);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, account_id INTEGER REFERENCES accounts(id), payload TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'pending', error TEXT, created_at TEXT NOT NULL, finished_at TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT NOT NULL, chat_id INTEGER, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pending_outbox ON outbox(state,id);
CREATE INDEX IF NOT EXISTS pending_jobs ON jobs(state,id);
CREATE UNIQUE INDEX IF NOT EXISTS retailcrm_action_node ON retailcrm_actions(instance_id,node_id);
CREATE INDEX IF NOT EXISTS image_jobs_queue ON image_jobs(state,next_poll_at,id);
"""


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA / "folio.db", timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _repair_items_mapping_fk(con):
    """Repair the FK left behind when the old offer mapping table was renamed."""

    targets = {
        row[2] for row in con.execute("PRAGMA foreign_key_list(items)").fetchall()
    }
    if "mappings_offer_legacy" not in targets:
        return
    con.execute(
        "CREATE TABLE items_fk_repair("
        "id INTEGER PRIMARY KEY,"
        "account_id INTEGER NOT NULL REFERENCES accounts(id),"
        "posting TEXT NOT NULL,"
        "order_number TEXT NOT NULL DEFAULT '',"
        "sku TEXT NOT NULL,"
        "offer_id TEXT NOT NULL,"
        "name TEXT NOT NULL,"
        "customer_name TEXT NOT NULL DEFAULT '',"
        "quantity INTEGER NOT NULL,"
        "external_status TEXT NOT NULL,"
        "order_started_at TEXT,"
        "mapping_id INTEGER REFERENCES mappings(id),"
        "UNIQUE(account_id,posting,sku))"
    )
    columns = (
        "id,account_id,posting,order_number,sku,offer_id,name,"
        "customer_name,quantity,external_status,order_started_at,mapping_id"
    )
    con.execute(f"INSERT INTO items_fk_repair({columns}) SELECT {columns} FROM items")
    con.execute("DROP TABLE items")
    con.execute("ALTER TABLE items_fk_repair RENAME TO items")
    if con.execute("PRAGMA foreign_key_check(items)").fetchone():
        raise sqlite3.IntegrityError("items mapping foreign key repair failed")


def _upgrade_order_chats(con):
    """Give each posting a Folio conversation, while retaining the Ozon chat ID."""

    columns = {row[1] for row in con.execute("PRAGMA table_info(chats)")}
    if "posting" not in columns:
        con.execute(
            "CREATE TABLE chats_by_order("
            "id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL REFERENCES accounts(id),"
            "external_id TEXT NOT NULL,posting TEXT,title TEXT NOT NULL,"
            "chat_type TEXT NOT NULL DEFAULT '',buyer_id TEXT NOT NULL DEFAULT '',"
            "buyer_name TEXT NOT NULL DEFAULT '',buyer_name_manual TEXT NOT NULL DEFAULT '',"
            "start_job_id INTEGER REFERENCES jobs(id),mode TEXT NOT NULL DEFAULT 'manual',"
            "epoch INTEGER NOT NULL DEFAULT 0,active_item INTEGER REFERENCES items(id),"
            "unread INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,"
            "UNIQUE(account_id,posting))"
        )
        con.execute(
            "INSERT INTO chats_by_order(id,account_id,external_id,posting,title,chat_type,"
            "buyer_id,buyer_name,buyer_name_manual,start_job_id,mode,epoch,active_item,"
            "unread,updated_at) SELECT c.id,c.account_id,c.external_id,"
            "CASE WHEN c.start_job_id IS NOT NULL THEN "
            "COALESCE((SELECT i.posting FROM items i WHERE i.id=c.active_item),"
            "(SELECT json_extract(j.payload,'$.posting') FROM jobs j WHERE j.id=c.start_job_id)) "
            "END,c.title,c.chat_type,c.buyer_id,c.buyer_name,c.buyer_name_manual,"
            "c.start_job_id,c.mode,c.epoch,c.active_item,c.unread,c.updated_at FROM chats c"
        )
        con.execute("DROP TABLE chats")
        con.execute("ALTER TABLE chats_by_order RENAME TO chats")
        # The old worker recorded a *confirmed* reused Ozon chat as unknown.
        # Only these exact legacy attempts are safe to resume after the
        # per-posting schema exists; transport-unknown starts stay untouched.
        con.execute(
            "UPDATE jobs SET state='pending',error=NULL,finished_at=NULL "
            "WHERE kind='start_chat' AND state='unknown' "
            "AND error='start_returned_existing_chat'"
        )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS chats_unowned_external "
        "ON chats(account_id,external_id) WHERE posting IS NULL"
    )


def initialize():
    with closing(connect()) as con:
        # Schema upgrades are performed while no application transaction is
        # active. References are restored before foreign keys are re-enabled.
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        con.execute("BEGIN IMMEDIATE")
        upgrades = {
            "items": {
                "order_number": "TEXT NOT NULL DEFAULT ''",
                "customer_name": "TEXT NOT NULL DEFAULT ''",
                "order_started_at": "TEXT",
            },
            "chats": {
                "chat_type": "TEXT NOT NULL DEFAULT ''",
                "buyer_id": "TEXT NOT NULL DEFAULT ''",
                "buyer_name": "TEXT NOT NULL DEFAULT ''",
                "buyer_name_manual": "TEXT NOT NULL DEFAULT ''",
                "start_job_id": "INTEGER REFERENCES jobs(id)",
            },
            "messages": {
                "order_number": "TEXT NOT NULL DEFAULT ''",
                "context_sku": "TEXT NOT NULL DEFAULT ''",
            },
            "users": {
                "local_login": "TEXT",
                "password_hash": "TEXT",
            },
            "outbox": {
                "kind": "TEXT NOT NULL DEFAULT 'text'",
                "media_id": "TEXT REFERENCES media(id)",
            },
            "instances": {"approval_due_at": "TEXT"},
            "retailcrm_actions": {"kind": "TEXT NOT NULL DEFAULT 'create'"},
            "retailcrm_integrations": {"statuses": "TEXT NOT NULL DEFAULT '[]'"},
            "image_jobs": {"instance_id": "INTEGER REFERENCES instances(id)", "node_id": "TEXT",
                           "media_ids": "TEXT NOT NULL DEFAULT '[]'"},
        }
        for table, expected in upgrades.items():
            present = {
                row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for column, declaration in expected.items():
                if column not in present:
                    con.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                    )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS image_jobs_scenario_node "
            "ON image_jobs(instance_id,node_id) WHERE instance_id IS NOT NULL"
        )
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_local_login ON users(local_login)")
        _upgrade_order_chats(con)
        columns = {
            row[1] for row in con.execute("PRAGMA table_info(mappings)").fetchall()
        }
        if "offer_id" in columns and "sku" not in columns:
            # The prototype keyed scenarios by offer_id. Preserve only links
            # that can be migrated to exactly one observed SKU; ambiguous
            # mappings must be configured explicitly instead of guessed.
            legacy = con.execute("SELECT * FROM mappings ORDER BY id").fetchall()
            transferable = []
            for mapping in legacy:
                skus = [
                    row[0]
                    for row in con.execute(
                        "SELECT DISTINCT sku FROM items WHERE mapping_id=? AND sku<>''",
                        (mapping["id"],),
                    ).fetchall()
                ]
                if len(skus) == 1:
                    transferable.append((mapping, skus[0]))
            con.execute("UPDATE items SET mapping_id=NULL")
            con.execute("ALTER TABLE mappings RENAME TO mappings_offer_legacy")
            con.execute(
                "CREATE TABLE mappings(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), sku TEXT NOT NULL, product_type TEXT NOT NULL, scenario_id INTEGER NOT NULL REFERENCES scenarios(id), template_ids TEXT NOT NULL DEFAULT '[]', UNIQUE(account_id,sku))"
            )
            for mapping, sku in transferable:
                con.execute(
                    "INSERT OR IGNORE INTO mappings(id,account_id,sku,product_type,scenario_id,template_ids) VALUES(?,?,?,?,?,?)",
                    (
                        mapping["id"],
                        mapping["account_id"],
                        sku,
                        mapping["product_type"],
                        mapping["scenario_id"],
                        mapping["template_ids"],
                    ),
                )
            con.execute("DROP TABLE mappings_offer_legacy")
            con.execute(
                "UPDATE items SET mapping_id=(SELECT m.id FROM mappings m WHERE m.account_id=items.account_id AND m.sku=items.sku)"
            )
        _repair_items_mapping_fk(con)
        mapping_columns = {
            row[1] for row in con.execute("PRAGMA table_info(mappings)").fetchall()
        }
        if "key_kind" not in mapping_columns:
            # Existing mappings used Ozon's numeric SKU. Convert only when an
            # observed order proves the seller article unambiguously.
            con.execute(
                "ALTER TABLE mappings ADD COLUMN key_kind TEXT NOT NULL DEFAULT 'seller_article'"
            )
            old_mappings = con.execute("SELECT id,account_id,sku FROM mappings").fetchall()
            for mapping in old_mappings:
                articles = [
                    row[0]
                    for row in con.execute(
                        "SELECT DISTINCT offer_id FROM items WHERE account_id=? "
                        "AND (mapping_id=? OR sku=?) AND offer_id<>''",
                        (mapping["account_id"], mapping["id"], mapping["sku"]),
                    )
                ]
                if len(articles) == 1 and not con.execute(
                    "SELECT 1 FROM mappings WHERE account_id=? AND sku=? AND id<>?",
                    (mapping["account_id"], articles[0], mapping["id"]),
                ).fetchone():
                    con.execute(
                        "UPDATE mappings SET sku=?,key_kind='seller_article' WHERE id=?",
                        (articles[0], mapping["id"]),
                    )
                else:
                    con.execute(
                        "UPDATE mappings SET key_kind='ozon_sku' WHERE id=?",
                        (mapping["id"],),
                    )
            con.execute(
                "UPDATE items SET mapping_id=(SELECT m.id FROM mappings m WHERE "
                "m.account_id=items.account_id AND m.sku=items.offer_id "
                "AND m.key_kind='seller_article') WHERE id NOT IN "
                "(SELECT item_id FROM instances)"
            )
        if "active" not in {row[1] for row in con.execute("PRAGMA table_info(mappings)")}:
            con.execute("ALTER TABLE mappings ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        con.execute(
            "UPDATE jobs SET error='legacy_probe_configuration' "
            "WHERE error='configure_http_limits'"
        )
        settings_row = con.execute("SELECT value FROM settings WHERE id=1").fetchone()
        settings = json.loads(settings_row[0])
        # Removed prototype-only transport controls. They are not product
        # settings and must never block a normal connection probe.
        changed = False
        transport_removed = False
        if settings.get("automation_enabled") and not settings.get("automation_enabled_at"):
            # A pre-existing enabled installation must not send greetings to
            # previously imported orders after this upgrade.
            settings["automation_enabled_at"] = now()
            changed = True
        for obsolete in (
            "http_timeout",
            "request_interval",
            "media_mimes",
            "media_max_bytes",
            "message_max_chars",
            "media_hosts",
        ):
            if obsolete in settings:
                settings.pop(obsolete)
                changed = True
                transport_removed = True
        if transport_removed:
            # A configuration created by the removed prototype must pass the
            # new capability checks again before any external write resumes.
            settings["automation_enabled"] = False
            settings["send_enabled"] = False
            settings["start_enabled"] = False
            settings.pop("automation_enabled_at", None)
        if "start_statuses" in settings:
            settings.pop("start_statuses")
            changed = True
        if settings.get("sync_enabled") and not all(
            settings.get(key) for key in ("sync_since", "sync_minutes")
        ):
            settings["sync_enabled"] = False
            changed = True
        if changed:
            con.execute("UPDATE settings SET value=? WHERE id=1", (dump(settings),))
        admin = os.environ.get("FOLIO_ADMIN_USER_ID")
        if admin:
            con.execute(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,'admin')",
                (admin, "Администратор Folio"),
            )
        con.commit()
        con.execute("PRAGMA foreign_keys=ON")


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
