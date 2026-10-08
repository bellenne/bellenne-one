"""Run separately: python -m app.worker. HTTP handlers only enqueue external I/O."""

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

from . import db, image_tasks, mattermost, media, presentation, retailcrm, scenarios
from .ozon import OzonAdapter, OzonError


def _text(value):
    return str(value).strip() if value is not None else ""


_OZON_IMAGE_MARKDOWN = re.compile(r"!\[[^\]\r\n]*\]\((https://[^\s)]+)\)")


def _message_contents(raw):
    """Ozon history can embed a file URL as a Markdown image in data text."""

    data = raw.get("data", [])
    if not isinstance(data, list) or not all(isinstance(part, str) for part in data):
        raise OzonError("message_data_contract_changed")
    text_parts, media_urls = [], []
    for part in data:
        if part.startswith("https://"):
            media_urls.append(part)
            continue
        media_urls.extend(match.group(1) for match in _OZON_IMAGE_MARKDOWN.finditer(part))
        remaining = _OZON_IMAGE_MARKDOWN.sub("", part).strip()
        if remaining:
            text_parts.append(remaining)
    return "\n".join(text_parts), media_urls


def _utc_timestamp(value):
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _new_order_after_activation(item, settings):
    order_time = _utc_timestamp(item["order_started_at"])
    enabled_time = _utc_timestamp(settings.get("automation_enabled_at"))
    return bool(order_time and enabled_time and order_time >= enabled_time)


def _person(value):
    """Return only an explicit marketplace person name; never infer one."""

    if not isinstance(value, dict):
        return "", ""
    identity = _text(value.get("id") or value.get("customer_id"))
    name = _text(value.get("name") or value.get("display_name"))
    if not name:
        name = " ".join(
            part
            for part in (
                _text(value.get("first_name")),
                _text(value.get("last_name")),
            )
            if part
        )
    return identity, name


def _buyer_from_chat(raw):
    value = raw.get("chat", raw) if isinstance(raw, dict) else {}
    candidates = []
    for source in (raw, value):
        if not isinstance(source, dict):
            continue
        for key in ("customer", "buyer"):
            if isinstance(source.get(key), dict):
                candidates.append(source[key])
        users = source.get("users")
        if isinstance(users, list):
            candidates.extend(user for user in users if isinstance(user, dict))
    for candidate in candidates:
        actor_type = _text(candidate.get("type")).casefold().replace("с", "c")
        if actor_type in {"customer", "buyer"} or not actor_type:
            identity, name = _person(candidate)
            if identity or name:
                return identity, name
    return "", ""


def _chat_type(raw):
    value = raw.get("chat", raw) if isinstance(raw, dict) else {}
    return _text(value.get("chat_type") if isinstance(value, dict) else "").upper()


def _message_context(raw):
    context = raw.get("context") if isinstance(raw, dict) else None
    if not isinstance(context, dict):
        return "", ""
    order = context.get("order") if isinstance(context.get("order"), dict) else {}
    item = context.get("item") if isinstance(context.get("item"), dict) else {}
    return (
        _text(context.get("order_number") or order.get("order_number")),
        _text(context.get("sku") or item.get("sku")),
    )


def _link_chat_context(con, chat, order_number, context_sku):
    if not order_number:
        return
    if chat["start_job_id"] is not None:
        # The start job already fixes the order/item context. A later chat
        # message must not silently attach another order to that conversation.
        return
    linked = con.execute(
        "SELECT id,customer_name FROM items WHERE account_id=? AND order_number=? AND (?='' OR sku=?)",
        (chat["account_id"], order_number, context_sku, context_sku),
    ).fetchall()
    for item in linked:
        con.execute(
            "INSERT OR IGNORE INTO chat_items(chat_id,item_id) VALUES(?,?)",
            (chat["id"], item["id"]),
        )
    if linked and not chat["active_item"]:
        count = con.execute(
            "SELECT COUNT(*) FROM chat_items WHERE chat_id=?", (chat["id"],)
        ).fetchone()[0]
        if count == 1:
            con.execute(
                "UPDATE chats SET active_item=? WHERE id=? AND active_item IS NULL",
                (linked[0]["id"], chat["id"]),
            )
    if linked and not chat["buyer_name"]:
        explicit_name = next(
            (row["customer_name"] for row in linked if row["customer_name"]),
            "",
        )
        if explicit_name:
            con.execute(
                "UPDATE chats SET buyer_name=? WHERE id=? AND buyer_name=''",
                (explicit_name, chat["id"]),
            )


def _managed_items(con, account_id, posting):
    return con.execute(
        "SELECT i.* FROM items i JOIN mappings m ON m.id=i.mapping_id "
        "JOIN scenarios s ON s.id=m.scenario_id "
        "WHERE i.account_id=? AND i.posting=? AND m.account_id=i.account_id "
        "AND m.key_kind='seller_article' AND m.active=1 AND m.sku=i.offer_id "
        "AND s.published IS NOT NULL ORDER BY i.id",
        (account_id, posting),
    ).fetchall()


def _account_can_start(con, account_id):
    row = con.execute(
        "SELECT capabilities FROM accounts WHERE id=? AND error IS NULL",
        (account_id,),
    ).fetchone()
    if not row:
        return False
    caps = json.loads(row[0])
    return all(caps.get(key) is True for key in (
        "account", "orders", "list"
    )) and all(caps.get(key) is not False for key in (
        "history", "send", "attachments", "start"
    ))


def close_cancelled_start_jobs(con, account_id=None, posting=None):
    """Retire obsolete chat-start attempts without trying the Ozon write again."""

    query = (
        "SELECT DISTINCT j.id FROM jobs j JOIN items i ON i.account_id=j.account_id "
        "AND i.posting=json_extract(j.payload,'$.posting') "
        "WHERE j.kind='start_chat' AND j.state IN ('pending','failed','unknown') "
        "AND i.external_status='cancelled' AND NOT EXISTS("
        "SELECT 1 FROM items other WHERE other.account_id=i.account_id "
        "AND other.posting=i.posting AND other.external_status<>'cancelled')"
    )
    params = []
    if account_id is not None:
        query += " AND j.account_id=?"
        params.append(account_id)
    if posting is not None:
        query += " AND i.posting=?"
        params.append(posting)
    for row in con.execute(query, params).fetchall():
        con.execute(
            "UPDATE jobs SET state='cancelled',finished_at=COALESCE(finished_at,?) "
            "WHERE id=?",
            (db.now(), row["id"]),
        )
        db.audit(con, "system", "integration.cancelled", "job", row["id"])


def import_order(con, account_id, posting):
    number = posting.get("posting_number")
    products = posting.get("products")
    if not number or not isinstance(products, list):
        raise OzonError("invalid_order_event")
    order_number = _text(posting.get("order_number"))
    order_started_at = _utc_timestamp(posting.get("in_process_at"))
    _, customer_name = _person(posting.get("customer"))
    if not customer_name:
        # Some delivery schemes expose the recipient rather than a separate
        # customer object. It is still an explicit marketplace name, not an
        # inferred value.
        _, customer_name = _person(posting.get("addressee"))
    external_status = _text(posting.get("status") or posting.get("status_alias"))
    for product in products:
        sku = _text(product.get("sku") or product.get("product_id"))
        offer_id = _text(product.get("offer_id") or product.get("product_offer_id"))
        product_name = _text(
            product.get("name") or product.get("product_name") or offer_id
        )
        try:
            quantity = int(product.get("quantity") or product.get("number_of_units"))
        except (TypeError, ValueError):
            quantity = 0
        if not sku or not offer_id or quantity < 1:
            raise OzonError("invalid_order_item")
        existing = con.execute(
            "SELECT * FROM items WHERE account_id=? AND posting=? AND sku=?",
            (account_id, number, sku),
        ).fetchone()
        mapping = con.execute(
            "SELECT id FROM mappings WHERE account_id=? AND sku=? AND key_kind='seller_article' AND active=1",
            (account_id, offer_id),
        ).fetchone()
        con.execute(
            "INSERT INTO items(account_id,posting,order_number,sku,offer_id,name,customer_name,quantity,external_status,order_started_at,mapping_id) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,posting,sku) DO UPDATE SET order_number=CASE WHEN excluded.order_number<>'' THEN excluded.order_number ELSE items.order_number END,external_status=excluded.external_status,quantity=excluded.quantity,name=excluded.name,customer_name=CASE WHEN excluded.customer_name<>'' THEN excluded.customer_name ELSE items.customer_name END,order_started_at=COALESCE(items.order_started_at,excluded.order_started_at),mapping_id=COALESCE(items.mapping_id,excluded.mapping_id)",
            (
                account_id,
                number,
                order_number,
                sku,
                offer_id,
                product_name,
                customer_name,
                quantity,
                external_status,
                order_started_at,
                mapping[0] if mapping else None,
            ),
        )
        if existing and (
            existing["external_status"] != external_status
            or existing["quantity"] != quantity
        ):
            db.audit(con, "system", "order.changed", "item", existing["id"])
        elif not existing:
            db.audit(con, "system", "order.imported", "posting", number)
        if external_status == "cancelled":
            item_id = existing["id"] if existing else con.execute(
                "SELECT id FROM items WHERE account_id=? AND posting=? AND sku=?",
                (account_id, number, sku),
            ).fetchone()[0]
            instance = con.execute(
                "SELECT id,status FROM instances WHERE item_id=?", (item_id,)
            ).fetchone()
            if instance and instance["status"] != "closed":
                con.execute(
                    "UPDATE instances SET status='closed',confirmed_at=NULL,"
                    "reviewed_at=NULL,ready_at=NULL WHERE id=?",
                    (instance["id"],),
                )
                con.execute(
                    "UPDATE outbox SET state='cancelled' WHERE instance_id=? "
                    "AND actor='bot' AND state='pending'",
                    (instance["id"],),
                )
                for event in con.execute(
                    "SELECT e.id,e.body,e.chat_id FROM events e JOIN chats c ON c.id=e.chat_id "
                    "WHERE c.active_item=? AND e.state='pending'",
                    (item_id,),
                ).fetchall():
                    if json.loads(event["body"]).get("item_id") == item_id:
                        con.execute(
                            "UPDATE events SET state='processed' WHERE id=?", (event["id"],)
                        )
                        db.audit(
                            con, "system", "event.ignored_cancelled", "event",
                            event["id"], event["chat_id"],
                        )
                con.execute(
                    "UPDATE chats SET mode='bot' WHERE active_item=? AND mode='manual' "
                    "AND NOT EXISTS(SELECT 1 FROM audit a WHERE a.chat_id=chats.id "
                    "AND a.action='chat.takeover' AND a.actor NOT IN ('system','ozon-seller') "
                    "AND a.id=(SELECT MAX(id) FROM audit WHERE chat_id=chats.id "
                    "AND action='chat.takeover'))",
                    (item_id,),
                )
                db.audit(con, "system", "order.cancelled", "item", item_id)
    if external_status == "cancelled":
        close_cancelled_start_jobs(con, account_id, number)


def import_chat(con, account_id, raw, posting=None):
    value = raw.get("chat", raw)
    external_id = value.get("chat_id")
    if not external_id:
        raise OzonError("invalid_chat_event")
    existing = con.execute(
        "SELECT id FROM chats WHERE account_id=? AND external_id=? AND posting IS ?",
        (account_id, external_id, posting),
    ).fetchone()
    chat_type = _chat_type(raw)
    buyer_id, buyer_name = _buyer_from_chat(raw)
    if existing:
        con.execute(
            "UPDATE chats SET chat_type=CASE WHEN ?<>'' THEN ? ELSE chat_type END,"
            "buyer_id=CASE WHEN ?<>'' THEN ? ELSE buyer_id END,"
            "buyer_name=CASE WHEN ?<>'' THEN ? ELSE buyer_name END WHERE id=?",
            (chat_type, chat_type, buyer_id, buyer_id, buyer_name, buyer_name, existing["id"]),
        )
    else:
        con.execute(
            "INSERT INTO chats(account_id,external_id,posting,title,chat_type,buyer_id,"
            "buyer_name,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (account_id, external_id, posting, str(external_id), chat_type,
             buyer_id, buyer_name, db.now()),
        )
    chat = con.execute(
        "SELECT * FROM chats WHERE account_id=? AND external_id=? AND posting IS ?",
        (account_id, external_id, posting),
    ).fetchone()
    if not existing:
        db.audit(con, "system", "chat.imported", "chat", chat["id"], chat["id"])
    return chat


def import_message(con, chat, raw, assets=None, attachment_error=False, *, route_event=True):
    external_id = str(raw.get("message_id", ""))
    if not external_id:
        raise OzonError("invalid_message_event")
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    actor_type = _text(user.get("type")).casefold().replace("с", "c")
    actor = (
        "buyer"
        if actor_type in {"customer", "buyer"}
        else "seller"
        if actor_type == "seller"
        else "external"
    )
    buyer_id, buyer_name = _person(user) if actor == "buyer" else ("", "")
    order_number, context_sku = _message_context(raw)
    if buyer_id or buyer_name:
        con.execute(
            "UPDATE chats SET buyer_id=CASE WHEN ?<>'' THEN ? ELSE buyer_id END,buyer_name=CASE WHEN ?<>'' THEN ? ELSE buyer_name END WHERE id=?",
            (buyer_id, buyer_id, buyer_name, buyer_name, chat["id"]),
        )
    _link_chat_context(con, chat, order_number, context_sku)
    chat = con.execute("SELECT * FROM chats WHERE id=?", (chat["id"],)).fetchone()
    existing = con.execute(
        "SELECT id FROM messages WHERE chat_id=? AND external_id=?",
        (chat["id"], external_id),
    ).fetchone()
    if existing:
        con.execute(
            "UPDATE messages SET actor=CASE WHEN ?<>'external' THEN ? ELSE actor END,order_number=CASE WHEN ?<>'' THEN ? ELSE order_number END,context_sku=CASE WHEN ?<>'' THEN ? ELSE context_sku END WHERE id=?",
            (
                actor,
                actor,
                order_number,
                order_number,
                context_sku,
                context_sku,
                existing["id"],
            ),
        )
        db.audit(
            con,
            "system",
            "message.duplicate",
            "message",
            external_id,
            chat["id"],
        )
        return
    # Do not store external media URLs in text history or event bodies.
    body, _ = _message_contents(raw)
    mids = []
    for content in assets or []:
        mids.append(
            media.store(con, content, db.config(con), chat["id"], chat["active_item"])
        )
    con.execute(
        "INSERT INTO messages(chat_id,external_id,actor,body,media_ids,order_number,context_sku,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            chat["id"],
            external_id,
            actor,
            body,
            db.dump(mids),
            order_number,
            context_sku,
            str(raw.get("created_at") or db.now()),
        ),
    )
    con.execute(
        "UPDATE chats SET unread=unread+?,updated_at=? WHERE id=?",
        (int(actor == "buyer"), db.now(), chat["id"]),
    )
    if actor == "buyer" and route_event:
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


def _comparable_chat_text(value):
    # Ozon adds Markdown hard-break spaces to lines in chat history.
    return re.sub(r"[ \t]+(?=\n|$)", "", value.replace("\r\n", "\n"))


_HISTORY_CLOCK_SKEW = timedelta(seconds=2)


def _not_before_send(sent_at, queued_at):
    # The real Ozon chat timestamp can precede our local outbox timestamp by
    # a fraction of a second. Text and message ID must still match uniquely.
    return sent_at >= queued_at - _HISTORY_CLOCK_SKEW


def reconcile_outbox(con, chat, raw_messages, file_digests=None):
    """Confirm uncertain sends only from one matching seller message in Ozon."""

    unknown = con.execute(
        "SELECT id,body,created_at,kind,media_id FROM outbox WHERE chat_id=? AND state='unknown'",
        (chat["id"],),
    ).fetchall()
    if not unknown:
        return
    claimed = {
        row[0] for row in con.execute(
            "SELECT external_id FROM outbox WHERE chat_id=? AND external_id IS NOT NULL",
            (chat["id"],),
        )
    }
    sellers = []
    for raw in raw_messages:
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        if _text(user.get("type")).casefold() != "seller":
            continue
        message_id = _text(raw.get("message_id"))
        created = _utc_timestamp(raw.get("created_at"))
        data = raw.get("data")
        if not message_id or message_id in claimed or not created or not isinstance(data, list):
            continue
        if not all(isinstance(part, str) for part in data):
            continue
        sellers.append((message_id, datetime.fromisoformat(created), _comparable_chat_text("\n".join(data))))
    candidates = {}
    for row in unknown:
        created = _utc_timestamp(row["created_at"])
        if not created:
            continue
        if row["kind"] == "file":
            asset = con.execute("SELECT digest FROM media WHERE id=? AND chat_id=?",
                                (row["media_id"], chat["id"])).fetchone()
            matches = [message_id for message_id, sent_at, _ in sellers
                       if asset and _not_before_send(sent_at, datetime.fromisoformat(created))
                       and (file_digests or {}).get(message_id) == asset["digest"]]
        else:
            body = _comparable_chat_text(row["body"])
            matches = [
                message_id for message_id, sent_at, text in sellers
                if _not_before_send(sent_at, datetime.fromisoformat(created)) and text == body
            ]
        if len(matches) == 1:
            candidates[row["id"]] = matches[0]
    recovered_message_ids = set()
    recovered_outbox_ids = []
    for outbox_id, message_id in candidates.items():
        if list(candidates.values()).count(message_id) != 1:
            continue
        con.execute(
            "UPDATE outbox SET state='sent',external_id=?,error=NULL "
            "WHERE id=? AND state='unknown'",
            (message_id, outbox_id),
        )
        db.audit(con, "system", "outbound.reconciled_auto", "outbox", outbox_id, chat["id"])
        recovered_message_ids.add(message_id)
        recovered_outbox_ids.append(outbox_id)
    if recovered_message_ids:
        _undo_false_takeover(con, chat, recovered_message_ids)
        for outbox_id in recovered_outbox_ids:
            scenarios.outgoing_confirmed(con, outbox_id)


def _undo_false_takeover(con, chat, recovered_message_ids):
    """Repair old chats paused only because Folio misread its own send."""

    current = con.execute("SELECT * FROM chats WHERE id=?", (chat["id"],)).fetchone()
    if not current or current["mode"] != "manual":
        return
    takeover = con.execute(
        "SELECT id,actor FROM audit WHERE chat_id=? AND action='chat.takeover' "
        "ORDER BY id DESC LIMIT 1",
        (chat["id"],),
    ).fetchone()
    if not takeover or takeover["actor"] != "ozon-seller":
        return
    following = con.execute(
        "SELECT action,object_id FROM audit WHERE chat_id=? AND id>? "
        "ORDER BY id LIMIT 1",
        (chat["id"], takeover["id"]),
    ).fetchone()
    if not following or (following["action"], following["object_id"]) not in {
        ("message.imported", message_id) for message_id in recovered_message_ids
    }:
        return
    instance = con.execute(
        "SELECT id,status,node,version_id FROM instances WHERE chat_id=? AND item_id=?",
        (chat["id"], current["active_item"]),
    ).fetchone()
    if not instance or instance["status"] not in {"collecting", "waiting_mockup", "waiting_integration"}:
        return
    if instance["status"] == "waiting_mockup" and not any(
        con.execute(
            "SELECT 1 FROM outbox WHERE chat_id=? AND instance_id=? AND kind='file' "
            "AND state='sent' AND external_id=?",
            (chat["id"], instance["id"], message_id),
        ).fetchone()
        for message_id in recovered_message_ids
    ):
        return
    if instance["status"] == "waiting_integration":
        graph = json.loads(con.execute(
            "SELECT graph FROM versions WHERE id=?", (instance["version_id"],)
        ).fetchone()[0])
        node = next((node for node in graph["nodes"] if node["id"] == instance["node"]), None)
        if not node or node["kind"] != "send_mockup" or not any(
            con.execute(
                "SELECT 1 FROM outbox WHERE chat_id=? AND instance_id=? AND actor='bot' "
                "AND kind='file' AND dedup=? AND state='sent' AND external_id=?",
                (chat["id"], instance["id"], f"mockup:{instance['id']}:{node['id']}", message_id),
            ).fetchone()
            for message_id in recovered_message_ids
        ):
            return
    if con.execute(
        "SELECT 1 FROM audit WHERE chat_id=? AND id>? "
        "AND actor NOT IN ('system') LIMIT 1",
        (chat["id"], takeover["id"]),
    ).fetchone() or con.execute(
        "SELECT 1 FROM outbox WHERE chat_id=? AND actor='manager' "
        "AND created_at>=(SELECT created_at FROM audit WHERE id=?) LIMIT 1",
        (chat["id"], takeover["id"]),
    ).fetchone():
        return
    con.execute("UPDATE chats SET mode='bot' WHERE id=? AND mode='manual'", (chat["id"],))
    for event in con.execute(
        "SELECT DISTINCT e.id,e.body FROM events e JOIN audit a "
        "ON a.object_type='event' AND a.object_id=CAST(e.id AS TEXT) "
        "AND a.action='event.processed' WHERE e.chat_id=? AND e.state='processed' "
        "AND a.id>? ORDER BY e.id",
        (chat["id"], takeover["id"]),
    ).fetchall():
        if json.loads(event["body"]).get("epoch") == current["epoch"]:
            con.execute("UPDATE events SET state='pending' WHERE id=?", (event["id"],))
    db.audit(con, "system", "chat.autorecovered", "chat", chat["id"], chat["id"])


def recover_sent_mockup(con, chat):
    """Resume a previously reconciled bot mockup after a false seller takeover."""

    current = con.execute("SELECT * FROM chats WHERE id=?", (chat["id"],)).fetchone()
    if not current or current["mode"] != "manual" or not current["active_item"]:
        return
    instance = con.execute(
        "SELECT * FROM instances WHERE chat_id=? AND item_id=? AND status='waiting_integration'",
        (chat["id"], current["active_item"]),
    ).fetchone()
    if not instance:
        return
    graph = json.loads(con.execute(
        "SELECT graph FROM versions WHERE id=?", (instance["version_id"],)
    ).fetchone()[0])
    node = next((node for node in graph["nodes"] if node["id"] == instance["node"]), None)
    if not node or node["kind"] != "send_mockup":
        return
    outgoing = con.execute(
        "SELECT id,external_id FROM outbox WHERE chat_id=? AND instance_id=? "
        "AND actor='bot' AND kind='file' AND dedup=? AND state='sent' "
        "AND external_id IS NOT NULL",
        (chat["id"], instance["id"], f"mockup:{instance['id']}:{node['id']}"),
    ).fetchone()
    if not outgoing:
        return
    _undo_false_takeover(con, chat, {outgoing["external_id"]})
    if con.execute("SELECT mode FROM chats WHERE id=?", (chat["id"],)).fetchone()[0] == "bot":
        scenarios.outgoing_confirmed(con, outgoing["id"])


def backfill_chat_orders(account_id, adapter):
    """Resolve order contexts to FBS postings with an exact Ozon filter."""

    checked_before = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with db.transaction() as con:
        missing = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT m.order_number FROM messages m "
                "JOIN chats c ON c.id=m.chat_id "
                "LEFT JOIN order_lookup_attempts lookup ON "
                "lookup.account_id=c.account_id AND lookup.order_number=m.order_number "
                "WHERE c.account_id=? AND c.chat_type='BUYER_SELLER' "
                "AND m.order_number<>'' "
                "AND NOT EXISTS(SELECT 1 FROM items i WHERE "
                "i.account_id=c.account_id AND i.order_number=m.order_number) "
                "AND (lookup.checked_at IS NULL OR lookup.checked_at<=?) "
                "ORDER BY m.order_number",
                (account_id, checked_before),
            ).fetchall()
        ]
    if missing:
        # The FBS list accepts exact order_numbers and at most one year per
        # request. Batching prevents one API call per historical chat.
        until = db.now()
        since = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        for start in range(0, len(missing), 50):
            batch = missing[start : start + 50]
            wanted = set(batch)
            for postings, _ in adapter.order_pages(
                since, until, order_numbers=batch
            ):
                with db.transaction() as con:
                    for posting in postings:
                        if _text(posting.get("order_number")) not in wanted:
                            raise OzonError("order_lookup_filter_contract_changed")
                        import_order(con, account_id, posting)
            with db.transaction() as con:
                con.executemany(
                    "INSERT INTO order_lookup_attempts(account_id,order_number,checked_at) "
                    "VALUES(?,?,?) ON CONFLICT(account_id,order_number) "
                    "DO UPDATE SET checked_at=excluded.checked_at",
                    [(account_id, order_number, db.now()) for order_number in batch],
                )
    with db.transaction() as con:
        contexts = con.execute(
            "SELECT DISTINCT m.chat_id,m.order_number,m.context_sku FROM messages m "
            "JOIN chats c ON c.id=m.chat_id WHERE c.account_id=? "
            "AND c.chat_type='BUYER_SELLER' AND m.order_number<>''",
            (account_id,),
        ).fetchall()
        for context in contexts:
            chat = con.execute(
                "SELECT * FROM chats WHERE id=?", (context["chat_id"],)
            ).fetchone()
            _link_chat_context(
                con, chat, context["order_number"], context["context_sku"]
            )


def _order_chat_for_message(con, order_chats, raw, file_digests=None):
    """Route one Ozon message to one order, never by buyer identity alone."""

    message_id = _text(raw.get("message_id"))
    if message_id:
        imported = [
            chat for chat in order_chats if con.execute(
                "SELECT 1 FROM messages WHERE chat_id=? AND external_id=?",
                (chat["id"], message_id),
            ).fetchone()
        ]
        if len(imported) == 1:
            return imported[0]
        if len(imported) > 1:
            return None
        sent = [
            chat for chat in order_chats if con.execute(
                "SELECT 1 FROM outbox WHERE chat_id=? AND external_id=?",
                (chat["id"], message_id),
            ).fetchone()
        ]
        if len(sent) == 1:
            return sent[0]
        if len(sent) > 1:
            return None
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    if _text(user.get("type")).casefold() == "seller":
        sent_at = _utc_timestamp(raw.get("created_at"))
        digest = (file_digests or {}).get(message_id)
        if digest and sent_at:
            file_matches = [
                chat for chat in order_chats for row in con.execute(
                    "SELECT o.created_at FROM outbox o JOIN media m ON m.id=o.media_id "
                    "WHERE o.chat_id=? AND o.kind='file' AND o.state='unknown' AND m.digest=?",
                    (chat["id"], digest),
                ) if (created := _utc_timestamp(row["created_at"]))
                and _not_before_send(datetime.fromisoformat(sent_at), datetime.fromisoformat(created))
            ]
            if len(file_matches) == 1:
                return file_matches[0]
            if len(file_matches) > 1:
                return None
        data = raw.get("data")
        if sent_at and isinstance(data, list) and all(isinstance(part, str) for part in data):
            body = _comparable_chat_text("\n".join(data))
            uncertain = [
                chat for chat in order_chats for row in con.execute(
                    "SELECT body,created_at FROM outbox WHERE chat_id=? AND state='unknown'",
                    (chat["id"],),
                ) if (created := _utc_timestamp(row["created_at"]))
                and _not_before_send(datetime.fromisoformat(sent_at), datetime.fromisoformat(created))
                and _comparable_chat_text(row["body"]) == body
            ]
            if len(uncertain) == 1:
                return uncertain[0]
            if len(uncertain) > 1:
                return None
    order_number, context_sku = _message_context(raw)
    if order_number:
        matching = [
            chat for chat in order_chats
            if order_number in {chat["item_order_number"], chat["posting"]}
            and (not context_sku or chat["item_sku"] == context_sku)
        ]
        return matching[0] if len(matching) == 1 else None
    created = _utc_timestamp(raw.get("created_at"))
    if not created:
        return None
    eligible = [chat for chat in order_chats if chat["start_at"]
                and (started := _utc_timestamp(chat["start_at"])) and created >= started]
    if len(eligible) == 1:
        return eligible[0]
    active = [
        chat for chat in eligible
        if chat["external_status"] != "cancelled"
        and chat["instance_status"] in {"collecting", "waiting_integration", "waiting_mockup", "waiting_approval", "needs_review", "waiting_release"}
    ]
    return active[0] if len(active) == 1 else None


def process_event(con, event):
    body = json.loads(event["body"])
    chat = con.execute("SELECT * FROM chats WHERE id=?", (event["chat_id"],)).fetchone()
    instance = con.execute(
        "SELECT * FROM instances WHERE chat_id=? AND item_id=?",
        (chat["id"], body["item_id"]),
    ).fetchone()
    if instance and instance["status"] == "closed" and con.execute(
        "SELECT 1 FROM items WHERE id=? AND external_status='cancelled'",
        (instance["item_id"],),
    ).fetchone():
        con.execute("UPDATE events SET state='processed' WHERE id=?", (event["id"],))
        db.audit(con, "system", "event.ignored_cancelled", "event", event["id"], chat["id"])
        return
    if instance and instance["status"] in {
        "waiting_integration",
        "waiting_mockup",
        "needs_review",
        "waiting_release",
        "ready",
    }:
        con.execute(
            "UPDATE instances SET status='needs_manager',confirmed_at=NULL,reviewed_at=NULL,ready_at=NULL WHERE id=?",
            (instance["id"],),
        )
        scenarios.takeover(con, chat["id"], "system")
    elif body.get("attachment_error"):
        scenarios.takeover(con, chat["id"], "system")
    elif instance and chat["mode"] == "bot" and chat["epoch"] == body["epoch"]:
        if con.execute(
            "SELECT 1 FROM outbox WHERE instance_id=? AND state IN ('pending','unknown')",
            (instance["id"],),
        ).fetchone():
            # Keep the incoming event pending until the send is confirmed from
            # Ozon history; this is not a manager handoff.
            return
        if con.execute(
            "SELECT 1 FROM outbox WHERE instance_id=? AND state='failed'",
            (instance["id"],),
        ).fetchone():
            scenarios.takeover(con, chat["id"], "system")
        else:
            scenarios.advance(con, instance["id"], body)
    con.execute("UPDATE events SET state='processed' WHERE id=?", (event["id"],))
    db.audit(con, "system", "event.processed", "event", event["id"], chat["id"])


def retailcrm_probe_job(adapter_factory=retailcrm.RetailCRMAdapter):
    with db.transaction() as con:
        record = retailcrm.integration(con)
        if not record:
            raise retailcrm.RetailCRMError("retailcrm_not_configured")
    adapter = None
    try:
        adapter = adapter_factory(record)
        capabilities = adapter.credentials()
        statuses = adapter.statuses() if capabilities.get("reference_read") else []
        error = next(
            (
                f"retailcrm_missing_{name}"
                for name in ("order_read", "order_write", "site")
                if not capabilities.get(name)
            ),
            None,
        )
    finally:
        if adapter:
            adapter.close()
    with db.transaction() as con:
        con.execute(
            "UPDATE retailcrm_integrations SET capabilities=?,statuses=?,checked_at=?,error=? WHERE id=1",
            (db.dump(capabilities), db.dump(statuses), db.now(), error),
        )
    return error


def finalize_retailcrm_action(action_id, state, error=None, retailcrm_id=None):
    with db.transaction() as con:
        action = con.execute(
            "SELECT * FROM retailcrm_actions WHERE id=?", (action_id,)
        ).fetchone()
        if not action:
            return
        instance = con.execute(
            "SELECT * FROM instances WHERE id=?", (action["instance_id"],)
        ).fetchone()
        con.execute(
            "UPDATE retailcrm_actions SET state=?,retailcrm_id=?,error=?,finished_at=? "
            "WHERE id=?",
            (state, retailcrm_id, error, db.now(), action_id),
        )
        if state == "failed" and error:
            fields = json.loads(instance["fields"])
            fields["integration_error"] = presentation.error_details(error)["title"]
            con.execute("UPDATE instances SET fields=? WHERE id=?", (db.dump(fields), instance["id"]))
        db.audit(
            con,
            "system",
            f"retailcrm.{state}",
            "retailcrm_action",
            action_id,
            instance["chat_id"],
        )
        if state in {"sent", "failed"}:
            scenarios.advance(con, action["instance_id"])
        else:
            con.execute(
                "UPDATE instances SET status='needs_manager' WHERE id=?",
                (action["instance_id"],),
            )
            scenarios.takeover(con, instance["chat_id"], "system")


def retailcrm_create_job(job, adapter_factory=retailcrm.RetailCRMAdapter):
    payload = json.loads(job["payload"])
    action_id = payload.get("action_id")
    with db.transaction() as con:
        action = con.execute(
            "SELECT * FROM retailcrm_actions WHERE id=?", (action_id,)
        ).fetchone()
        record = retailcrm.integration(con)
        if not action or not record:
            raise retailcrm.RetailCRMError("retailcrm_not_configured")
        capabilities = json.loads(record["capabilities"])
        if not record["checked_at"] or not all(
            capabilities.get(name) for name in ("order_read", "order_write", "site")
        ):
            raise retailcrm.RetailCRMError("retailcrm_not_verified")
        instance = con.execute(
            "SELECT * FROM instances WHERE id=?", (action["instance_id"],)
        ).fetchone()
        graph = json.loads(
            con.execute(
                "SELECT graph FROM versions WHERE id=?", (instance["version_id"],)
            ).fetchone()[0]
        )
        node = next(
            candidate
            for candidate in graph["nodes"]
            if candidate["id"] == action["node_id"]
        )
        desired_status = str(node.get("status") or "").strip()
        if desired_status and desired_status not in {
            row.get("code") for row in json.loads(record["statuses"])
        }:
            raise retailcrm.RetailCRMError("retailcrm_status_unavailable")
        external_id = retailcrm.external_id(instance["id"])
    adapter = None
    state, error, retailcrm_id = "unknown", None, None
    try:
        adapter = adapter_factory(record)
        existing = adapter.find_order(external_id)
        if existing is None:
            with db.transaction() as con:
                _, order = retailcrm.order_payload(con, instance["id"], node)
            with db.transaction() as con:
                con.execute(
                    "UPDATE retailcrm_actions SET state='unknown' WHERE id=?",
                    (action_id,),
                )
            result = adapter.create_order(order)
        else:
            result = existing
        order_data = result.get("order") if isinstance(result, dict) else None
        value = result.get("id") if isinstance(result, dict) else None
        if not isinstance(value, int) and isinstance(order_data, dict):
            value = order_data.get("id")
        if not isinstance(value, int):
            raise retailcrm.RetailCRMError("retailcrm_invalid_response")
        retailcrm_id = value
        state = "sent"
    except retailcrm.RetailCRMError as exc:
        state = "unknown" if exc.unknown else "failed"
        error = exc.code
    except Exception:  # noqa: BLE001 -- never expose credentials or response bodies
        state, error = "unknown", "retailcrm_internal_unknown"
    finally:
        if adapter:
            adapter.close()

    finalize_retailcrm_action(action_id, state, error, retailcrm_id)
    return state, error


def retailcrm_note_job(job, adapter_factory=retailcrm.RetailCRMAdapter):
    action_id = json.loads(job["payload"]).get("action_id")
    with db.transaction() as con:
        action = con.execute("SELECT * FROM retailcrm_actions WHERE id=? AND kind='note'",
                             (action_id,)).fetchone()
        record = retailcrm.integration(con)
        if not action or not record:
            raise retailcrm.RetailCRMError("retailcrm_not_configured")
        capabilities = json.loads(record["capabilities"])
        if not record["checked_at"] or not all(capabilities.get(name) for name in
                                               ("order_read", "order_write", "site")):
            raise retailcrm.RetailCRMError("retailcrm_not_verified")
        instance = con.execute("SELECT * FROM instances WHERE id=?",
                               (action["instance_id"],)).fetchone()
        graph = json.loads(con.execute("SELECT graph FROM versions WHERE id=?",
                                      (instance["version_id"],)).fetchone()[0])
        node = next(candidate for candidate in graph["nodes"] if candidate["id"] == action["node_id"])
        external_id, payload = retailcrm.order_payload(con, instance["id"], node)
        note = payload["managerComment"]
        desired_status = str(node.get("status") or "").strip()
        if desired_status and desired_status not in {
            row.get("code") for row in json.loads(record["statuses"])
        }:
            raise retailcrm.RetailCRMError("retailcrm_status_unavailable")
    adapter = None
    state, error, retailcrm_id = "unknown", None, None
    try:
        adapter = adapter_factory(record)
        current = adapter.find_order(external_id)
        order = current.get("order") if isinstance(current, dict) else None
        if not isinstance(order, dict):
            raise retailcrm.RetailCRMError("retailcrm_order_missing")
        old_comment = order.get("customerComment") or ""
        if not isinstance(old_comment, str):
            raise retailcrm.RetailCRMError("retailcrm_invalid_response")
        items = order.get("items") or []
        if not isinstance(items, list):
            raise retailcrm.RetailCRMError("retailcrm_items_unverified")
        status_change = bool(desired_status and desired_status != order.get("status"))
        if note not in old_comment or status_change:
            with db.transaction() as con:
                con.execute("UPDATE retailcrm_actions SET state='unknown' WHERE id=?",
                            (action_id,))
            comment = (old_comment + "\n\n" + note).strip() if note not in old_comment else old_comment
            if status_change:
                adapter.edit_order_customer_comment(external_id, comment, items, desired_status)
            else:
                adapter.edit_order_customer_comment(external_id, comment, items)
        retailcrm_id = order.get("id") if isinstance(order.get("id"), int) else None
        state = "sent"
    except retailcrm.RetailCRMError as exc:
        state, error = ("unknown" if exc.unknown else "failed"), exc.code
    except Exception:  # noqa: BLE001 -- do not expose CRM payloads
        state, error = "unknown", "retailcrm_internal_unknown"
    finally:
        if adapter:
            adapter.close()
    finalize_retailcrm_action(action_id, state, error, retailcrm_id)
    return state, error


def finalize_mattermost_action(action_id, state, error=None):
    with db.transaction() as con:
        action = con.execute("SELECT * FROM mattermost_actions WHERE id=?", (action_id,)).fetchone()
        if not action:
            return
        instance = con.execute("SELECT * FROM instances WHERE id=?", (action["instance_id"],)).fetchone()
        con.execute(
            "UPDATE mattermost_actions SET state=?,error=?,finished_at=? WHERE id=?",
            (state, error, db.now(), action_id),
        )
        db.audit(con, "system", f"mattermost.{state}", "mattermost_action", action_id, instance["chat_id"])
        if state == "failed" and error:
            fields = json.loads(instance["fields"])
            fields["integration_error"] = presentation.error_details(error)["title"]
            con.execute("UPDATE instances SET fields=? WHERE id=?", (db.dump(fields), instance["id"]))
        if state in {"sent", "failed"}:
            scenarios.advance(con, instance["id"])
        else:
            con.execute("UPDATE instances SET status='needs_manager' WHERE id=?", (instance["id"],))
            scenarios.takeover(con, instance["chat_id"], "system")


def mattermost_send_job(job, sender=None):
    sender = sender or mattermost.send
    action_id = json.loads(job["payload"]).get("action_id")
    with db.transaction() as con:
        action = con.execute("SELECT * FROM mattermost_actions WHERE id=?", (action_id,)).fetchone()
        record = mattermost.integration(con)
        if not action or not record:
            raise mattermost.MattermostError("mattermost_not_configured")
        instance = con.execute("SELECT * FROM instances WHERE id=?", (action["instance_id"],)).fetchone()
        graph = json.loads(con.execute("SELECT graph FROM versions WHERE id=?", (instance["version_id"],)).fetchone()[0])
        node = next(candidate for candidate in graph["nodes"] if candidate["id"] == action["node_id"])
        try:
            _, payload = retailcrm.order_payload(con, instance["id"], {"comment": node["message"]})
            message = payload["managerComment"]
        except retailcrm.RetailCRMError as exc:
            raise mattermost.MattermostError("mattermost_template_invalid") from exc
        if not message:
            raise mattermost.MattermostError("mattermost_message_empty")
        # Any crash after this point leaves an unknown delivery, never an automatic resend.
        con.execute("UPDATE mattermost_actions SET state='unknown' WHERE id=?", (action_id,))
    state, error = "unknown", None
    try:
        sender(record, message)
        state = "sent"
    except mattermost.MattermostError as exc:
        state, error = ("unknown" if exc.unknown else "failed"), exc.code
    except Exception:
        state, error = "unknown", "mattermost_internal_unknown"
    finalize_mattermost_action(action_id, state, error)
    return state, error


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
        db.audit(con, "system", "outbound.attempt", "outbox", row["id"], chat["id"])
        if chat["epoch"] != row["epoch"] or (
            row["actor"] == "bot" and chat["mode"] != "bot"
        ):
            con.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (row["id"],))
            db.audit(
                con,
                "system",
                "outbound.cancelled",
                "outbox",
                row["id"],
                chat["id"],
            )
            return True
        if row["kind"] == "file":
            file_owner = con.execute(
                "SELECT m.item_id,i.external_status FROM media m "
                "LEFT JOIN items i ON i.id=m.item_id WHERE m.id=? AND m.chat_id=?",
                (row["media_id"], chat["id"]),
            ).fetchone()
            if file_owner and file_owner["item_id"] and (
                file_owner["item_id"] != chat["active_item"]
                or file_owner["external_status"] == "cancelled"
            ):
                con.execute(
                    "UPDATE outbox SET state='cancelled',error='outbound_order_unavailable' WHERE id=?",
                    (row["id"],),
                )
                db.audit(con, "system", "outbound.cancelled", "outbox", row["id"], chat["id"])
                return True
        caps = json.loads(account["capabilities"])
        if not settings.get("send_enabled") or caps.get("send") is False:
            con.execute(
                "UPDATE outbox SET state='failed',error='chat_api_not_enabled' WHERE id=?",
                (row["id"],),
            )
            db.audit(
                con,
                "system",
                "outbound.failed",
                "outbox",
                row["id"],
                chat["id"],
            )
            return True
        adapter = None
        try:
            adapter = adapter_factory(account, settings)
            if row["kind"] == "file":
                asset = con.execute("SELECT * FROM media WHERE id=? AND chat_id=?",
                                    (row["media_id"], chat["id"])).fetchone()
                path = db.DATA / "media" / row["media_id"]
                if not asset or not path.is_file():
                    raise OzonError("outbound_file_missing")
                filename = "folio-image." + {"image/jpeg": "jpg", "image/png": "png",
                                             "image/webp": "webp"}[asset["mime"]]
                external_id = adapter.send_file(chat["external_id"], filename, path.read_bytes())
            else:
                external_id = adapter.send(chat["external_id"], row["body"])
            con.execute(
                "UPDATE outbox SET state='sent',external_id=?,error=NULL WHERE id=?",
                (external_id, row["id"]),
            )
            con.execute(
                "INSERT OR IGNORE INTO messages(chat_id,external_id,actor,body,media_ids,created_at) VALUES(?,?,?,?,?,?)",
                (chat["id"], external_id, row["actor"], row["body"],
                 db.dump([row["media_id"]] if row["kind"] == "file" else []), db.now()),
            )
            scenarios.outgoing_confirmed(con, row["id"])
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
        final_state = con.execute(
            "SELECT state FROM outbox WHERE id=?", (row["id"],)
        ).fetchone()[0]
        if final_state == "unknown":
            db.job(con, "sync", chat["account_id"])
        db.audit(
            con,
            "system",
            f"outbound.{final_state}",
            "outbox",
            row["id"],
            chat["id"],
        )
    return True


def probe_job(job, adapter):
    """Check read capabilities without importing data or sending anything."""

    account_id = job["account_id"]
    with db.transaction() as con:
        row = con.execute(
            "SELECT capabilities FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        capabilities = json.loads(row[0])
    for name in ("account", "orders", "list", "history"):
        capabilities.pop(name, None)
        capabilities.pop(f"{name}_error", None)

    def check(name, operation):
        try:
            result = operation()
        except OzonError as exc:
            capabilities[name] = False
            capabilities[f"{name}_error"] = exc.code
            return None
        capabilities[name] = True
        capabilities.pop(f"{name}_error", None)
        return result

    if check("account", adapter.seller_info) is not None:
        until = datetime.now(timezone.utc)
        since = until - timedelta(days=1)
        check(
            "orders",
            lambda: adapter.probe_orders(since.isoformat(), until.isoformat()),
        )
        chat_page = check("list", lambda: adapter.chat_page(limit=100))
        chats = chat_page.get("chats", []) if chat_page else []
        buyer_chat = next(
            (candidate for candidate in chats if _chat_type(candidate) == "BUYER_SELLER"),
            None,
        )
        if buyer_chat:
            value = buyer_chat.get("chat", buyer_chat)
            chat_id = value.get("chat_id") if isinstance(value, dict) else None
            if chat_id:
                check("history", lambda: adapter.history_page(chat_id, limit=1))

    first_error = next(
        (
            capabilities.get(f"{name}_error")
            for name in ("account", "orders", "list", "history")
            if capabilities.get(name) is False
        ),
        None,
    )
    with db.transaction() as con:
        con.execute(
            "UPDATE accounts SET capabilities=?,checked_at=?,error=? WHERE id=?",
            (db.dump(capabilities), db.now(), first_error, account_id),
        )
    return first_error


def sync_job(job, adapter, settings):
    account_id = job["account_id"]
    payload = json.loads(job["payload"])
    if job["kind"] == "catalog_sync":
        products = {}
        for product in adapter.products():
            offer_id = _text(product.get("offer_id"))
            if offer_id:
                products[offer_id] = (
                    account_id,
                    offer_id,
                    _text(product.get("sku")),
                    _text(product.get("product_id")),
                )
        with db.transaction() as con:
            con.execute("DELETE FROM ozon_products WHERE account_id=?", (account_id,))
            con.executemany(
                "INSERT INTO ozon_products(account_id,offer_id,ozon_sku,product_id) "
                "VALUES(?,?,?,?)",
                products.values(),
            )
        return
    if job["kind"] == "sync":
        adapter.seller_info()
        with db.transaction() as con:
            row = con.execute(
                "SELECT capabilities FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            caps = json.loads(row[0])
            caps["account"] = True
            con.execute(
                "UPDATE accounts SET capabilities=? WHERE id=?",
                (db.dump(caps), account_id),
            )
        since = payload.get("since") or settings.get("sync_since")
        if not since:
            raise OzonError("configure_sync_since")
        until, cursor = db.now(), ""
        with db.transaction() as con:
            checkpoint = con.execute(
                "SELECT orders_cursor FROM sync_state WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if checkpoint and checkpoint[0]:
                saved = json.loads(checkpoint[0])
                since, until, cursor = (
                    saved["since"],
                    saved["until"],
                    saved.get("cursor", saved.get("offset", "")),
                )
        for postings, next_cursor in adapter.order_pages(since, until, cursor):
            with db.transaction() as con:
                for posting in postings:
                    import_order(con, account_id, posting)
                cursor = (
                    db.dump(
                        {"since": since, "until": until, "cursor": next_cursor}
                    )
                    if next_cursor is not None
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
        if not all(settings.get(key) for key in (
            "start_enabled", "automation_enabled", "send_enabled"
        )):
            raise OzonError("chat_start_not_enabled")
        with db.transaction() as con:
            if not _account_can_start(con, account_id):
                raise OzonError("chat_start_not_verified")
            items = _managed_items(con, account_id, payload["posting"])
            if len(items) != 1:
                raise OzonError("start_item_ambiguous")
            if not _new_order_after_activation(items[0], settings):
                raise OzonError("order_before_automation")
            if items[0]["external_status"] == "cancelled":
                raise OzonError("order_cancelled")
            if con.execute(
                "SELECT 1 FROM chat_items ci JOIN chats c ON c.id=ci.chat_id "
                "WHERE ci.item_id=? AND c.start_job_id IS NOT NULL",
                (items[0]["id"],),
            ).fetchone():
                return
        # Ozon may return the same physical chat for another posting from this
        # buyer. A new Folio conversation is still required for this order.
        external_id = adapter.start(payload["posting"])
        with db.transaction() as con:
            chat = import_chat(
                con,
                account_id,
                {"chat_id": external_id, "chat_type": "BUYER_SELLER"},
                posting=payload["posting"],
            )
            if chat["start_job_id"] is not None and chat["start_job_id"] != job["id"]:
                raise OzonError("start_returned_existing_chat", unknown=True)
            con.execute(
                "UPDATE chats SET start_job_id=?,active_item=?,mode='bot' WHERE id=?",
                (job["id"], items[0]["id"], chat["id"]),
            )
            con.execute(
                "INSERT OR IGNORE INTO chat_items(chat_id,item_id) VALUES(?,?)",
                (chat["id"], items[0]["id"]),
            )
            caps = json.loads(
                con.execute(
                    "SELECT capabilities FROM accounts WHERE id=?", (account_id,)
                ).fetchone()[0]
            )
            caps["start"] = True
            caps.pop("start_error", None)
            con.execute(
                "UPDATE accounts SET capabilities=? WHERE id=?",
                (db.dump(caps), account_id),
            )
            if not con.execute(
                "SELECT 1 FROM instances WHERE item_id=?", (items[0]["id"],)
            ).fetchone():
                scenarios.start(con, items[0]["id"], chat["id"])
            inst = con.execute(
                "SELECT id FROM instances WHERE item_id=?", (items[0]["id"],)
            ).fetchone()
            scenarios.advance(con, inst[0])
        return
    history_checked = False
    attachments_checked = False
    for raw_chat in adapter.chats():
        # The list also contains Seller Support and other operational chats.
        # They have no buyer/order context and must never appear in Folio's
        # customer inbox.
        if _chat_type(raw_chat) != "BUYER_SELLER":
            with db.transaction() as con:
                value = raw_chat.get("chat", raw_chat)
                external_id = value.get("chat_id") if isinstance(value, dict) else None
                if external_id:
                    con.execute(
                        "UPDATE chats SET chat_type=? WHERE account_id=? AND external_id=?",
                        (_chat_type(raw_chat), account_id, external_id),
                    )
            continue
        value = raw_chat.get("chat", raw_chat)
        external_id = value.get("chat_id") if isinstance(value, dict) else None
        with db.transaction() as con:
            known = con.execute(
                "SELECT c.*,COALESCE(j.finished_at,j.created_at) AS start_at,i.external_status,"
                "i.order_number AS item_order_number,i.sku AS item_sku,"
                "inst.status AS instance_status FROM chats c "
                "JOIN jobs j ON j.id=c.start_job_id "
                "JOIN items i ON i.id=c.active_item "
                "LEFT JOIN instances inst ON inst.chat_id=c.id AND inst.item_id=i.id "
                "WHERE c.account_id=? AND c.external_id=? AND c.posting IS NOT NULL "
                "ORDER BY j.created_at,c.id",
                (account_id, external_id),
            ).fetchall()
            if not known:
                continue
            for chat in known:
                import_chat(con, account_id, raw_chat, posting=chat["posting"])
        raw_messages = list(adapter.history(external_id))
        history_checked = True
        file_digests_all = {}
        with db.transaction() as con:
            has_unknown_file = any(con.execute(
                "SELECT 1 FROM outbox WHERE chat_id=? AND kind='file' AND state='unknown'",
                (chat["id"],),
            ).fetchone() for chat in known)
        if has_unknown_file:
            for raw in raw_messages:
                user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
                if _text(user.get("type")).casefold() != "seller":
                    continue
                _, urls = _message_contents(raw)
                if len(urls) != 1:
                    continue
                try:
                    content = media.download(urls[0], settings,
                                             auth_client=getattr(adapter, "client", None))
                    file_digests_all[_text(raw.get("message_id"))] = hashlib.sha256(content).hexdigest()
                except Exception:  # noqa: BLE001 -- uncertain until a safe comparison succeeds
                    pass
        with db.transaction() as con:
            routed = {}
            for raw in raw_messages:
                target = _order_chat_for_message(con, known, raw, file_digests_all)
                if target:
                    routed.setdefault(target["id"], []).append(raw)
            ambiguous = []
            for raw in raw_messages:
                if _order_chat_for_message(con, known, raw, file_digests_all) is not None:
                    continue
                user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
                if _text(user.get("type")).casefold() not in {"buyer", "customer"}:
                    continue
                if _message_context(raw)[0]:
                    continue
                created = _utc_timestamp(raw.get("created_at"))
                active = [chat for chat in known if created and chat["start_at"]
                          and (started := _utc_timestamp(chat["start_at"])) and created >= started
                          and chat["external_status"] != "cancelled"
                          and chat["instance_status"] in {"collecting", "waiting_integration", "waiting_mockup", "waiting_approval", "needs_review", "waiting_release"}]
                if len(active) > 1:
                    ambiguous.append((raw, active))
            for raw, active in ambiguous:
                for chat in active:
                    current = con.execute("SELECT * FROM chats WHERE id=?", (chat["id"],)).fetchone()
                    if not con.execute("SELECT 1 FROM messages WHERE chat_id=? AND external_id=?",
                                       (chat["id"], _text(raw.get("message_id")))).fetchone():
                        import_message(con, current, raw, route_event=False)
                        scenarios.takeover(con, chat["id"], "system")
                        db.audit(con, "system", "chat.ambiguous_order_reply", "message",
                                 _text(raw.get("message_id")), chat["id"])
        for chat in known:
            scoped_messages = routed.get(chat["id"], [])
            with db.transaction() as con:
                reconcile_outbox(con, chat, scoped_messages, file_digests_all)
                recover_sent_mockup(con, chat)
            if not scoped_messages:
                continue
            # The endpoint returns newest first; apply chronologically.
            for raw in reversed(scoped_messages):
                with db.transaction() as con:
                    duplicate = con.execute(
                        "SELECT 1 FROM messages WHERE chat_id=? AND external_id=?",
                        (chat["id"], str(raw.get("message_id", ""))),
                    ).fetchone()
                    current = con.execute(
                        "SELECT * FROM chats WHERE id=?", (chat["id"],)
                    ).fetchone()
                    if duplicate:
                        import_message(con, current, raw)
                        continue
                assets, failed = [], False
                _, media_urls = _message_contents(raw)
                for url in media_urls:
                    try:
                        assets.append(media.download(url, settings, auth_client=getattr(adapter, "client", None)))
                        attachments_checked = True
                    except Exception:  # noqa: BLE001 -- redact external failures at the worker boundary
                        failed = True
                with db.transaction() as con:
                    latest = con.execute(
                        "SELECT * FROM chats WHERE id=?", (chat["id"],)
                    ).fetchone()
                    if latest["epoch"] != current["epoch"]:
                        assets, failed = [], True
                    import_message(con, latest, raw, assets, failed)
    # Folio-started chats are linked to the exact posting before history is
    # processed. Historical, externally started chats are outside this inbox.
    with db.transaction() as con:
        caps = json.loads(
            con.execute(
                "SELECT capabilities FROM accounts WHERE id=?", (account_id,)
            ).fetchone()[0]
        )
        caps["list"] = True
        if history_checked:
            caps["history"] = True
        if attachments_checked:
            caps["attachments"] = True
        con.execute(
            "UPDATE accounts SET capabilities=?,checked_at=?,error=NULL WHERE id=?",
            (db.dump(caps), db.now(), account_id),
        )
        if all(settings.get(key) for key in (
            "start_enabled", "automation_enabled", "send_enabled"
        )) and _account_can_start(con, account_id):
            # One durable attempt per posting. Never retry an uncertain start.
            postings = con.execute(
                "SELECT DISTINCT i.posting FROM items i JOIN mappings m ON m.id=i.mapping_id "
                "JOIN scenarios s ON s.id=m.scenario_id WHERE i.account_id=? "
                "AND m.account_id=i.account_id AND m.key_kind='seller_article' AND m.active=1 "
                "AND m.sku=i.offer_id AND s.published IS NOT NULL",
                (account_id,),
            ).fetchall()
            for row in postings:
                managed = _managed_items(con, account_id, row["posting"])
                if len(managed) != 1 or not _new_order_after_activation(managed[0], settings):
                    continue
                if managed[0]["external_status"] == "cancelled":
                    continue
                if con.execute(
                    "SELECT 1 FROM chat_items ci JOIN chats c ON c.id=ci.chat_id "
                    "WHERE ci.item_id=? AND c.start_job_id IS NOT NULL",
                    (managed[0]["id"],),
                ).fetchone():
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
        close_cancelled_start_jobs(con)
        scenarios.release_due(con)
        event = con.execute(
            "SELECT e.* FROM events e WHERE e.state='pending' AND (NOT EXISTS("
            "SELECT 1 FROM outbox o WHERE o.chat_id=e.chat_id "
            "AND o.state IN ('pending','unknown')) OR EXISTS("
            "SELECT 1 FROM instances i JOIN items item ON item.id=i.item_id "
            "WHERE i.chat_id=e.chat_id AND i.item_id=CASE WHEN json_valid(e.body) "
            "THEN json_extract(e.body,'$.item_id') END "
            "AND i.status='closed' AND item.external_status='cancelled')) "
            "ORDER BY e.id LIMIT 1"
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
        scenarios.expire_approvals(con)
        job = con.execute(
            "SELECT j.* FROM jobs j LEFT JOIN sync_state s ON s.account_id=j.account_id WHERE j.state='pending' AND (s.blocked_until IS NULL OR s.blocked_until<=?) ORDER BY j.id LIMIT 1",
            (db.now(),),
        ).fetchone()
        if job:
            con.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
            if job["kind"] in {"retailcrm_create", "retailcrm_note"}:
                action_id = json.loads(job["payload"]).get("action_id")
                con.execute(
                    "UPDATE retailcrm_actions SET state='running' WHERE id=? AND state='pending'",
                    (action_id,),
                )
            if job["kind"] == "mattermost_send":
                action_id = json.loads(job["payload"]).get("action_id")
                con.execute("UPDATE mattermost_actions SET state='running' WHERE id=? AND state='pending'", (action_id,))
            account = con.execute(
                "SELECT * FROM accounts WHERE id=?", (job["account_id"],)
            ).fetchone()
            settings = db.config(con)
    if job:
        adapter = None
        retry_after = None
        try:
            if job["kind"] == "retailcrm_probe":
                error = retailcrm_probe_job()
                state = "failed" if error else "done"
            elif job["kind"] == "retailcrm_create":
                action_state, error = retailcrm_create_job(job)
                state = "done" if action_state == "sent" else action_state
            elif job["kind"] == "retailcrm_note":
                action_state, error = retailcrm_note_job(job)
                state = "done" if action_state == "sent" else action_state
            elif job["kind"] == "mattermost_send":
                action_state, error = mattermost_send_job(job)
                state = "done" if action_state == "sent" else action_state
            else:
                adapter = OzonAdapter(account, settings)
            if job["kind"] == "probe":
                error = probe_job(job, adapter)
                state = "failed" if error else "done"
            elif job["kind"] not in {"retailcrm_probe", "retailcrm_create", "retailcrm_note", "mattermost_send"}:
                sync_job(job, adapter, settings)
                state, error = "done", None
        except retailcrm.RetailCRMError as exc:
            state, error = ("unknown" if exc.unknown else "failed"), exc.code
            if job["kind"] in {"retailcrm_create", "retailcrm_note"}:
                action_id = json.loads(job["payload"]).get("action_id")
                finalize_retailcrm_action(action_id, state, error)
        except mattermost.MattermostError as exc:
            state, error = ("unknown" if exc.unknown else "failed"), exc.code
            if job["kind"] == "mattermost_send":
                action_id = json.loads(job["payload"]).get("action_id")
                finalize_mattermost_action(action_id, state, error)
        except OzonError as exc:
            state, error = ("unknown" if exc.unknown else "failed"), exc.code
            retry_after = exc.retry_after
        except Exception:  # noqa: BLE001 -- redact external failures at the worker boundary
            state, error = (
                (
                    "unknown"
                    if job["kind"] in {"start_chat", "retailcrm_create", "retailcrm_note", "mattermost_send"}
                    else "failed"
                ),
                (
                    "retailcrm_internal_unknown"
                    if job["kind"] in {"retailcrm_create", "retailcrm_note"}
                    else "mattermost_internal_unknown" if job["kind"] == "mattermost_send"
                    else "worker_internal_error"
                ),
            )
            if job["kind"] in {"retailcrm_create", "retailcrm_note"}:
                action_id = json.loads(job["payload"]).get("action_id")
                finalize_retailcrm_action(action_id, state, error)
            if job["kind"] == "mattermost_send":
                action_id = json.loads(job["payload"]).get("action_id")
                finalize_mattermost_action(action_id, state, error)
        finally:
            if adapter:
                adapter.close()
        with db.transaction() as con:
            con.execute(
                "UPDATE jobs SET state=?,error=?,finished_at=? WHERE id=?",
                (state, error, db.now(), job["id"]),
            )
            if job["kind"] == "start_chat" and error == "ozon_http_403":
                caps = json.loads(con.execute(
                    "SELECT capabilities FROM accounts WHERE id=?", (job["account_id"],)
                ).fetchone()[0])
                caps["start"] = False
                caps["start_error"] = error
                con.execute(
                    "UPDATE accounts SET capabilities=? WHERE id=?",
                    (db.dump(caps), job["account_id"]),
                )
            if job["kind"] == "retailcrm_probe" and error:
                con.execute(
                    "UPDATE retailcrm_integrations SET error=?,statuses='[]',checked_at=? WHERE id=1",
                    (error, db.now()),
                )
            # A single order's chat-start failure is not a failed account
            # connection or sync; keep it on the start job for reconciliation.
            if error and job["kind"] in {"probe", "sync"}:
                con.execute(
                    "UPDATE accounts SET error=? WHERE id=?",
                    (error, job["account_id"]),
                )
                if job["kind"] == "sync":
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
            elif job["kind"] == "sync":
                con.execute(
                    "INSERT INTO sync_state(account_id,last_success) VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET failures=0,blocked_until=NULL,last_success=excluded.last_success",
                    (job["account_id"], db.now()),
                )
            db.audit(con, "system", f"integration.{state}", "job", job["id"])
        return True
    if send_one():
        return True
    return False


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
            "UPDATE jobs SET state=CASE WHEN kind IN ('start_chat','retailcrm_create','retailcrm_note','mattermost_send') "
            "THEN 'unknown' ELSE 'failed' END,error='worker_interrupted' "
            "WHERE state='running'"
        )
        con.execute(
            "UPDATE retailcrm_actions SET state='unknown',error='worker_interrupted',"
            "finished_at=? WHERE state='running'",
            (db.now(),),
        )
        con.execute(
            "UPDATE mattermost_actions SET state='unknown',error='worker_interrupted',finished_at=? WHERE state='running'",
            (db.now(),),
        )
        # POST /v1/jobs has no idempotency key. A crash after sending it must
        # never silently submit the same image again.
        con.execute(
            "UPDATE image_jobs SET state='unknown',error='image_worker_submission_unknown',updated_at=? "
            "WHERE state='submitting'",
            (db.now(),),
        )
        for action in con.execute(
            "SELECT m.instance_id,i.chat_id FROM mattermost_actions m "
            "JOIN instances i ON i.id=m.instance_id "
            "WHERE m.state='unknown' AND i.status='waiting_integration'"
        ).fetchall():
            con.execute("UPDATE instances SET status='needs_manager' WHERE id=?", (action["instance_id"],))
            scenarios.takeover(con, action["chat_id"], "system")
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
