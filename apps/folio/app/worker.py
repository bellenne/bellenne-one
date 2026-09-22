"""Run separately: python -m app.worker. HTTP handlers only enqueue external I/O."""

import json
import os
import time
from datetime import datetime, timedelta, timezone

from . import db, media, scenarios
from .ozon import OzonAdapter, OzonError


def import_order(con, account_id, posting):
    number = posting.get("posting_number")
    products = posting.get("products")
    if not number or not isinstance(products, list):
        raise OzonError("invalid_order_event")
    for product in products:
        if (
            not product.get("sku")
            or not product.get("offer_id")
            or not isinstance(product.get("quantity"), int)
            or product["quantity"] < 1
        ):
            raise OzonError("invalid_order_item")
        existing = con.execute(
            "SELECT * FROM items WHERE account_id=? AND posting=? AND sku=?",
            (account_id, number, str(product["sku"])),
        ).fetchone()
        mapping = con.execute(
            "SELECT id FROM mappings WHERE account_id=? AND offer_id=?",
            (account_id, product["offer_id"]),
        ).fetchone()
        external_status = str(posting.get("status", ""))
        con.execute(
            "INSERT INTO items(account_id,posting,sku,offer_id,name,quantity,external_status,mapping_id) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account_id,posting,sku) DO UPDATE SET external_status=excluded.external_status,quantity=excluded.quantity,name=excluded.name,mapping_id=COALESCE(items.mapping_id,excluded.mapping_id)",
            (
                account_id,
                number,
                str(product["sku"]),
                product["offer_id"],
                product.get("name", product["offer_id"]),
                product["quantity"],
                external_status,
                mapping[0] if mapping else None,
            ),
        )
        if existing and (
            existing["external_status"] != external_status
            or existing["quantity"] != product["quantity"]
        ):
            for linked in con.execute(
                "SELECT chat_id FROM chat_items WHERE item_id=?", (existing["id"],)
            ).fetchall():
                scenarios.takeover(con, linked[0], "system")
            con.execute(
                "UPDATE instances SET status='needs_manager',confirmed_at=NULL,reviewed_at=NULL,ready_at=NULL WHERE item_id=?",
                (existing["id"],),
            )
            db.audit(con, "system", "order.changed", "item", existing["id"])
        elif not existing:
            db.audit(con, "system", "order.imported", "posting", number)


def import_chat(con, account_id, raw):
    value = raw.get("chat", raw)
    external_id = value.get("chat_id")
    if not external_id:
        raise OzonError("invalid_chat_event")
    con.execute(
        "INSERT INTO chats(account_id,external_id,title,updated_at) VALUES(?,?,?,?) ON CONFLICT(account_id,external_id) DO NOTHING",
        (account_id, external_id, str(external_id), db.now()),
    )
    return con.execute(
        "SELECT * FROM chats WHERE account_id=? AND external_id=?",
        (account_id, external_id),
    ).fetchone()


def import_message(con, chat, raw, assets=None, attachment_error=False):
    external_id = str(raw.get("message_id", ""))
    if not external_id:
        raise OzonError("invalid_message_event")
    if con.execute(
        "SELECT 1 FROM messages WHERE chat_id=? AND external_id=?",
        (chat["id"], external_id),
    ).fetchone():
        return
    actor_type = (raw.get("user") or {}).get("type")
    actor = (
        "buyer"
        if actor_type == "Customer"
        else "seller"
        if actor_type == "Seller"
        else "external"
    )
    data = raw.get("data", [])
    if not isinstance(data, list) or not all(isinstance(s, str) for s in data):
        raise OzonError("message_data_contract_changed")
    # Do not store signed external media URLs in logs or text history.
    body = "\n".join(s for s in data if not s.startswith("https://"))
    mids = []
    for content in assets or []:
        mids.append(
            media.store(con, content, db.config(con), chat["id"], chat["active_item"])
        )
    con.execute(
        "INSERT INTO messages(chat_id,external_id,actor,body,media_ids,created_at) VALUES(?,?,?,?,?,?)",
        (
            chat["id"],
            external_id,
            actor,
            body,
            db.dump(mids),
            str(raw.get("created_at") or db.now()),
        ),
    )
    con.execute(
        "UPDATE chats SET unread=unread+?,updated_at=? WHERE id=?",
        (int(actor == "buyer"), db.now(), chat["id"]),
    )
    if actor == "buyer":
        event = {
            "text": body,
            "media_ids": mids,
            "item_id": chat["active_item"],
            "epoch": chat["epoch"],
            "attachment_error": attachment_error,
        }
        con.execute(
            "INSERT OR IGNORE INTO events(account_id,external_key,chat_id,body) VALUES(?,?,?,?)",
            (
                chat["account_id"],
                f"message:{chat['external_id']}:{external_id}",
                chat["id"],
                db.dump(event),
            ),
        )
    elif (
        actor == "seller"
        and not con.execute(
            "SELECT 1 FROM outbox WHERE chat_id=? AND external_id=? AND state='sent'",
            (chat["id"], external_id),
        ).fetchone()
    ):
        scenarios.takeover(con, chat["id"], "ozon-seller")
    db.audit(con, "system", "message.imported", "message", external_id, chat["id"])


def process_event(con, event):
    body = json.loads(event["body"])
    chat = con.execute("SELECT * FROM chats WHERE id=?", (event["chat_id"],)).fetchone()
    instance = con.execute(
        "SELECT * FROM instances WHERE chat_id=? AND item_id=?",
        (chat["id"], body["item_id"]),
    ).fetchone()
    if instance and instance["status"] in {"needs_review", "waiting_release", "ready"}:
        con.execute(
            "UPDATE instances SET status='needs_manager',confirmed_at=NULL,reviewed_at=NULL,ready_at=NULL WHERE id=?",
            (instance["id"],),
        )
        scenarios.takeover(con, chat["id"], "system")
    elif body.get("attachment_error"):
        scenarios.takeover(con, chat["id"], "system")
    elif instance and chat["mode"] == "bot" and chat["epoch"] == body["epoch"]:
        if con.execute(
            "SELECT 1 FROM outbox WHERE instance_id=? AND state IN ('pending','unknown','failed')",
            (instance["id"],),
        ).fetchone():
            scenarios.takeover(con, chat["id"], "system")
        else:
            scenarios.advance(con, instance["id"], body)
    con.execute("UPDATE events SET state='processed' WHERE id=?", (event["id"],))
    db.audit(con, "system", "event.processed", "event", event["id"], chat["id"])


def send_one(adapter_factory=OzonAdapter):
    # Durable unknown BEFORE I/O. A process crash cannot turn a possibly sent
    # message back into pending. The subsequent write lock is also the takeover
    # serialization boundary (single-worker MVP; no in-request network calls).
    with db.transaction() as con:
        row = con.execute(
            "SELECT o.* FROM outbox o WHERE o.state='pending' AND NOT EXISTS(SELECT 1 FROM outbox u WHERE u.chat_id=o.chat_id AND u.state='unknown') ORDER BY o.id LIMIT 1"
        ).fetchone()
        if not row:
            return False
        con.execute("UPDATE outbox SET state='unknown' WHERE id=?", (row["id"],))
    with db.transaction() as con:
        chat = con.execute(
            "SELECT * FROM chats WHERE id=?", (row["chat_id"],)
        ).fetchone()
        settings = db.config(con)
        account = con.execute(
            "SELECT * FROM accounts WHERE id=?", (chat["account_id"],)
        ).fetchone()
        if chat["epoch"] != row["epoch"] or (
            row["actor"] == "bot" and chat["mode"] != "bot"
        ):
            con.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (row["id"],))
            return True
        caps = json.loads(account["capabilities"])
        if not settings.get("send_enabled") or not caps.get("history"):
            con.execute(
                "UPDATE outbox SET state='failed',error='chat_api_not_enabled' WHERE id=?",
                (row["id"],),
            )
            return True
        adapter = None
        try:
            adapter = adapter_factory(account, settings)
            external_id = adapter.send(chat["external_id"], row["body"])
            con.execute(
                "UPDATE outbox SET state='sent',external_id=?,error=NULL WHERE id=?",
                (external_id, row["id"]),
            )
            con.execute(
                "INSERT OR IGNORE INTO messages(chat_id,external_id,actor,body,created_at) VALUES(?,?,?,?,?)",
                (chat["id"], external_id, row["actor"], row["body"], db.now()),
            )
            caps["send"] = True
            con.execute(
                "UPDATE accounts SET capabilities=? WHERE id=?",
                (db.dump(caps), account["id"]),
            )
        except OzonError as exc:
            con.execute(
                "UPDATE outbox SET state=?,error=? WHERE id=?",
                ("unknown" if exc.unknown else "failed", exc.code, row["id"]),
            )
            if exc.code in {"ozon_http_401", "ozon_http_403"}:
                con.execute(
                    "UPDATE accounts SET capabilities='{}',error=? WHERE id=?",
                    (exc.code, account["id"]),
                )
        except Exception:  # noqa: BLE001 -- redact external failures at the worker boundary
            con.execute(
                "UPDATE outbox SET error='send_internal_unknown' WHERE id=?",
                (row["id"],),
            )
        finally:
            if adapter:
                adapter.close()
        db.audit(con, "system", "outbound.attempt", "outbox", row["id"], chat["id"])
    return True


def sync_job(job, adapter, settings):
    account_id = job["account_id"]
    payload = json.loads(job["payload"])
    if job["kind"] in {"sync", "probe"}:
        since = payload.get("since") or settings.get("sync_since")
        if not since:
            raise OzonError("configure_sync_since")
        until, offset = db.now(), 0
        if job["kind"] == "sync":
            with db.transaction() as con:
                checkpoint = con.execute(
                    "SELECT orders_cursor FROM sync_state WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                if checkpoint and checkpoint[0]:
                    saved = json.loads(checkpoint[0])
                    since, until, offset = (
                        saved["since"],
                        saved["until"],
                        saved["offset"],
                    )
        for postings, next_offset in adapter.order_pages(since, until, offset):
            if job["kind"] == "sync":
                with db.transaction() as con:
                    for posting in postings:
                        import_order(con, account_id, posting)
                    cursor = (
                        db.dump({"since": since, "until": until, "offset": next_offset})
                        if next_offset is not None
                        else None
                    )
                    con.execute(
                        "INSERT INTO sync_state(account_id,orders_cursor) VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET orders_cursor=excluded.orders_cursor",
                        (account_id, cursor),
                    )
        with db.transaction() as con:
            account = con.execute(
                "SELECT capabilities FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            caps = json.loads(account[0])
            caps["orders"] = True
            con.execute(
                "UPDATE accounts SET capabilities=? WHERE id=?",
                (db.dump(caps), account_id),
            )
    if job["kind"] == "start_chat":
        if not settings.get("start_enabled"):
            raise OzonError("chat_start_not_enabled")
        with db.transaction() as con:
            eligible = con.execute(
                "SELECT external_status FROM items WHERE account_id=? AND posting=?",
                (account_id, payload["posting"]),
            ).fetchall()
            if not eligible or any(
                row[0] not in settings.get("start_statuses", []) for row in eligible
            ):
                raise OzonError("order_status_not_enabled")
        # Start is also a write: failed/unknown jobs require explicit resolution.
        external_id = adapter.start(payload["posting"])
        with db.transaction() as con:
            chat = import_chat(con, account_id, {"chat_id": external_id})
            items = con.execute(
                "SELECT * FROM items WHERE account_id=? AND posting=?",
                (account_id, payload["posting"]),
            ).fetchall()
            for item in items:
                con.execute(
                    "INSERT OR IGNORE INTO chat_items VALUES(?,?)",
                    (chat["id"], item["id"]),
                )
        prior_history = list(adapter.history(external_id))
        with db.transaction() as con:
            caps = json.loads(
                con.execute(
                    "SELECT capabilities FROM accounts WHERE id=?", (account_id,)
                ).fetchone()[0]
            )
            caps.update({"start": True, "history": True})
            con.execute(
                "UPDATE accounts SET capabilities=? WHERE id=?",
                (db.dump(caps), account_id),
            )
            if len(items) == 1 and items[0]["mapping_id"]:
                con.execute(
                    "UPDATE chats SET active_item=? WHERE id=?",
                    (items[0]["id"], chat["id"]),
                )
                if not con.execute(
                    "SELECT 1 FROM instances WHERE item_id=?", (items[0]["id"],)
                ).fetchone():
                    scenarios.start(con, items[0]["id"], chat["id"])
                if settings.get("automation_enabled") and not prior_history:
                    con.execute("UPDATE chats SET mode='bot' WHERE id=?", (chat["id"],))
                    inst = con.execute(
                        "SELECT id FROM instances WHERE item_id=?", (items[0]["id"],)
                    ).fetchone()
                    scenarios.advance(con, inst[0])
        return
    history_checked = False
    for raw_chat in adapter.chats():
        with db.transaction() as con:
            chat = import_chat(con, account_id, raw_chat)
        raw_messages = list(adapter.history(chat["external_id"]))
        history_checked = True
        if job["kind"] == "probe":
            break
        # The endpoint returns newest first; apply chronologically, deduplicating
        # using real message IDs, independently of their possibly very large size.
        for raw in reversed(raw_messages):
            with db.transaction() as con:
                if con.execute(
                    "SELECT 1 FROM messages WHERE chat_id=? AND external_id=?",
                    (chat["id"], str(raw.get("message_id", ""))),
                ).fetchone():
                    continue
                current = con.execute(
                    "SELECT * FROM chats WHERE id=?", (chat["id"],)
                ).fetchone()
            assets, failed = [], False
            for value in raw.get("data", []):
                if isinstance(value, str) and value.startswith("https://"):
                    try:
                        assets.append(media.download(value, settings))
                    except Exception:  # noqa: BLE001 -- redact external failures at the worker boundary
                        failed = True
            with db.transaction() as con:
                # Never attach to a context that changed while downloading.
                latest = con.execute(
                    "SELECT * FROM chats WHERE id=?", (chat["id"],)
                ).fetchone()
                if latest["epoch"] != current["epoch"]:
                    assets, failed = [], True
                import_message(con, latest, raw, assets, failed)
    with db.transaction() as con:
        caps = json.loads(
            con.execute(
                "SELECT capabilities FROM accounts WHERE id=?", (account_id,)
            ).fetchone()[0]
        )
        caps.update({"list": True, "history": history_checked})
        con.execute(
            "UPDATE accounts SET capabilities=?,checked_at=?,error=NULL WHERE id=?",
            (db.dump(caps), db.now(), account_id),
        )
        if job["kind"] == "sync" and settings.get("start_enabled"):
            # One durable attempt per posting. Never retry an uncertain start.
            postings = con.execute(
                "SELECT DISTINCT posting,external_status FROM items WHERE account_id=? AND mapping_id IS NOT NULL AND id NOT IN (SELECT item_id FROM chat_items)",
                (account_id,),
            ).fetchall()
            for row in postings:
                if row["external_status"] not in settings.get("start_statuses", []):
                    continue
                encoded = db.dump({"posting": row[0]})
                if not con.execute(
                    "SELECT 1 FROM jobs WHERE account_id=? AND kind='start_chat' AND payload=?",
                    (account_id, encoded),
                ).fetchone():
                    con.execute(
                        "INSERT INTO jobs(kind,account_id,payload,created_at) VALUES('start_chat',?,?,?)",
                        (account_id, encoded, db.now()),
                    )


def tick():
    with db.transaction() as con:
        scenarios.release_due(con)
        event = con.execute(
            "SELECT * FROM events WHERE state='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if event:
            con.execute("SAVEPOINT event_processing")
            try:
                process_event(con, event)
            except Exception:  # noqa: BLE001 -- redact external failures at the worker boundary
                con.execute("ROLLBACK TO event_processing")
                con.execute(
                    "UPDATE events SET state='failed',error='event_processing_failed' WHERE id=?",
                    (event["id"],),
                )
                scenarios.takeover(con, event["chat_id"], "system")
                db.audit(
                    con,
                    "system",
                    "event.failed",
                    "event",
                    event["id"],
                    event["chat_id"],
                )
            con.execute("RELEASE event_processing")
            return True
        job = con.execute(
            "SELECT j.* FROM jobs j LEFT JOIN sync_state s ON s.account_id=j.account_id WHERE j.state='pending' AND (s.blocked_until IS NULL OR s.blocked_until<=?) ORDER BY j.id LIMIT 1",
            (db.now(),),
        ).fetchone()
        if job:
            con.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
            account = con.execute(
                "SELECT * FROM accounts WHERE id=?", (job["account_id"],)
            ).fetchone()
            settings = db.config(con)
    if job:
        adapter = None
        retry_after = None
        try:
            adapter = OzonAdapter(account, settings)
            sync_job(job, adapter, settings)
            state, error = "done", None
        except OzonError as exc:
            state, error = ("unknown" if exc.unknown else "failed"), exc.code
            retry_after = exc.retry_after
        except Exception:  # noqa: BLE001 -- redact external failures at the worker boundary
            state, error = (
                ("unknown" if job["kind"] == "start_chat" else "failed"),
                "worker_internal_error",
            )
        finally:
            if adapter:
                adapter.close()
        with db.transaction() as con:
            con.execute(
                "UPDATE jobs SET state=?,error=?,finished_at=? WHERE id=?",
                (state, error, db.now(), job["id"]),
            )
            if error:
                con.execute(
                    "UPDATE accounts SET error=?,capabilities='{}' WHERE id=?",
                    (error, job["account_id"]),
                )
                con.execute(
                    "INSERT INTO sync_state(account_id,failures) VALUES(?,1) ON CONFLICT(account_id) DO UPDATE SET failures=failures+1",
                    (job["account_id"],),
                )
                failures = con.execute(
                    "SELECT failures FROM sync_state WHERE account_id=?",
                    (job["account_id"],),
                ).fetchone()[0]
                delay = max(
                    retry_after or 0,
                    (settings.get("sync_minutes") or 0)
                    * 60
                    * 2 ** min(failures - 1, 6),
                )
                if delay:
                    until = (
                        datetime.now(timezone.utc) + timedelta(seconds=delay)
                    ).isoformat()
                    con.execute(
                        "UPDATE sync_state SET blocked_until=? WHERE account_id=?",
                        (until, job["account_id"]),
                    )
            else:
                con.execute(
                    "INSERT INTO sync_state(account_id,last_success) VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET failures=0,blocked_until=NULL,last_success=excluded.last_success",
                    (job["account_id"], db.now()),
                )
            db.audit(con, "system", f"integration.{state}", "job", job["id"])
        return True
    return send_one()


def schedule():
    with db.transaction() as con:
        settings = db.config(con)
        minutes = settings.get("sync_minutes")
        if not settings.get("sync_enabled") or not minutes:
            return
        threshold = (
            datetime.now(timezone.utc) - timedelta(minutes=minutes)
        ).isoformat()
        for account in con.execute("SELECT id FROM accounts"):
            latest = con.execute(
                "SELECT created_at FROM jobs WHERE account_id=? AND kind='sync' ORDER BY id DESC LIMIT 1",
                (account[0],),
            ).fetchone()
            if not latest or latest[0] < threshold:
                db.job(con, "sync", account[0])


def main():
    db.initialize()
    with db.transaction() as con:
        con.execute(
            "UPDATE jobs SET state=CASE WHEN kind='start_chat' THEN 'unknown' ELSE 'failed' END,error='worker_interrupted' WHERE state='running'"
        )
    while True:
        try:
            schedule()
            busy = tick()
        except Exception:  # noqa: BLE001 -- redact external failures at the worker boundary
            # No traceback can leak credentials or response bodies.
            print("folio_worker_tick_failed", flush=True)
            busy = False
        if not busy:
            time.sleep(float(os.environ.get("FOLIO_WORKER_IDLE_SECONDS", "1")))


if __name__ == "__main__":
    from .locking import worker_lock

    with worker_lock():
        main()
