"""Execute the real interpreter in a private in-memory DB; never run a worker."""

import io
import sqlite3

from PIL import Image

from . import db, media, scenarios


def simulate(graph, product_type, answers):
    scenarios.validate(graph, product_type)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    con.execute(
        "UPDATE settings SET value=?", (db.dump({"message_max_chars": 100000}),)
    )
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
            "INSERT INTO mappings(id,account_id,offer_id,product_type,scenario_id,template_ids) VALUES(1,1,'SIMULATION',?,1,'[1]')",
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
        scenarios.advance(con, 1)
        trace = []
        for answer in answers:
            content = {"text": answer, "media_ids": []}
            if answer.startswith("photo:"):
                try:
                    count = int(answer.split(":", 1)[1])
                    if not 1 <= count <= 9:
                        raise ValueError()
                except ValueError:
                    raise scenarios.Invalid(
                        "В симуляции используйте photo:1 … photo:9"
                    ) from None
                image = io.BytesIO()
                Image.new("RGB", (1, 1)).save(image, format="PNG")
                for _ in range(count):
                    mid = media.store(
                        con,
                        image.getvalue(),
                        {
                            "media_max_bytes": len(image.getvalue()),
                            "media_mimes": ["image/png"],
                        },
                        1,
                        1,
                    )
                    files.append(db.DATA / "media" / mid)
                    content["media_ids"].append(mid)
                content["text"] = ""
            scenarios.advance(con, 1, content)
            row = con.execute(
                "SELECT node,status,fields FROM instances WHERE id=1"
            ).fetchone()
            trace.append({"answer": answer, **dict(row)})
        return {
            "trace": trace,
            "messages": [
                dict(r) for r in con.execute("SELECT body FROM outbox ORDER BY id")
            ],
            "instance": dict(
                con.execute("SELECT * FROM instances WHERE id=1").fetchone()
            ),
        }
    finally:
        con.close()
        for path in files:
            path.unlink(missing_ok=True)
