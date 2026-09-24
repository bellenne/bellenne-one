"""Execute the real interpreter in a private in-memory DB; never run a worker."""

import io
import sqlite3

from PIL import Image

from . import db, media, scenarios


def _event(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.startswith("photo:"):
        try:
            return {"kind": "photo", "count": int(value.split(":", 1)[1])}
        except ValueError:
            return {"kind": "photo", "count": 0}
    return {"kind": "text", "value": str(value)}


def simulate(graph, product_type, answers):
    """Run the real engine against a synthetic order and return a chat timeline."""
    scenarios.normalize(graph, product_type)
    scenarios.validate(graph, product_type)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    files = []
    try:
        con.execute(
            "INSERT INTO accounts(id,name,client_id,secret) VALUES(1,'simulation','simulation','unused')"
        )
        con.execute(
            "INSERT INTO scenarios(id,name,product_type,draft) VALUES(1,'simulation',?,?)",
            (product_type, db.dump(graph)),
        )
        scenarios.publish(con, 1, "simulation")
        con.execute("INSERT INTO templates(id,name) VALUES(1,'Тестовый шаблон')")
        con.execute(
            "INSERT INTO mappings(id,account_id,sku,product_type,scenario_id,template_ids) VALUES(1,1,'1',?,1,'[1]')",
            (product_type,),
        )
        con.execute(
            "INSERT INTO items(id,account_id,posting,sku,offer_id,name,quantity,external_status,mapping_id) VALUES(1,1,'SIMULATION','1','SIMULATION','Тестовая позиция',1,'simulation',1)"
        )
        con.execute(
            "INSERT INTO chats(id,account_id,external_id,title,mode,active_item,updated_at) VALUES(1,1,'simulation','simulation','bot',1,?)",
            (db.now(),),
        )
        con.execute("INSERT INTO chat_items VALUES(1,1)")
        scenarios.start(con, 1, 1)
        scenarios.advance(con, 1, simulate_external=True)
        trace = []
        timeline = []
        last_outbox = 0

        def collect_bot_messages():
            nonlocal last_outbox
            rows = con.execute(
                "SELECT id,body FROM outbox WHERE id>? ORDER BY id", (last_outbox,)
            ).fetchall()
            for row in rows:
                timeline.append({"actor": "bot", "body": row["body"], "kind": "text"})
                last_outbox = row["id"]

        collect_bot_messages()
        index = {node["id"]: node for node in graph["nodes"]}
        for raw_answer in answers:
            event = _event(raw_answer)
            before = con.execute("SELECT node FROM instances WHERE id=1").fetchone()
            current = index.get(before["node"], {})
            content = {"text": "", "media_ids": []}
            answer_label = ""
            if event.get("kind") == "photo":
                count = event.get("count")
                if not isinstance(count, int) or not 1 <= count <= 9:
                    raise scenarios.Invalid("За один шаг можно добавить от 1 до 9 тестовых фото")
                image = io.BytesIO()
                Image.new("RGB", (1, 1)).save(image, format="PNG")
                for _ in range(count):
                    mid = media.store(
                        con,
                        image.getvalue(),
                        {},
                        1,
                        1,
                    )
                    files.append(db.DATA / "media" / mid)
                    content["media_ids"].append(mid)
                answer_label = (
                    "Добавлено тестовое фото"
                    if count == 1
                    else f"Добавлено тестовых фото: {count}"
                )
                timeline.append(
                    {"actor": "buyer", "body": answer_label, "kind": "photo"}
                )
            elif event.get("kind") == "finish_photos":
                content["text"] = str(current.get("accept", "")).strip()
                if not content["text"]:
                    raise scenarios.Invalid(
                        "В текущем шаге не настроен ответ для завершения загрузки фото"
                    )
                answer_label = content["text"]
                timeline.append(
                    {"actor": "buyer", "body": answer_label, "kind": "text"}
                )
            elif event.get("kind") == "text":
                content["text"] = str(event.get("value", "")).strip()
                answer_label = content["text"]
                timeline.append(
                    {"actor": "buyer", "body": answer_label, "kind": "text"}
                )
            else:
                raise scenarios.Invalid("Неизвестное событие тестового диалога")
            scenarios.advance(con, 1, content, simulate_external=True)
            collect_bot_messages()
            row = con.execute(
                "SELECT node,status,fields FROM instances WHERE id=1"
            ).fetchone()
            trace.append({"answer": answer_label, **dict(row)})
        instance = dict(con.execute("SELECT * FROM instances WHERE id=1").fetchone())
        current_node = index.get(instance["node"])
        return {
            "trace": trace,
            "timeline": timeline,
            "messages": [
                dict(r) for r in con.execute("SELECT body FROM outbox ORDER BY id")
            ],
            "instance": instance,
            "current_node": current_node,
            "templates": [
                dict(row)
                for row in con.execute(
                    "SELECT id,name FROM templates WHERE active=1 ORDER BY id"
                ).fetchall()
            ],
        }
    finally:
        con.close()
        for path in files:
            path.unlink(missing_ok=True)
