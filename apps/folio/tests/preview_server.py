"""Local, synthetic UI fixture. No worker, no real keys, no production auth bypass.

Run from apps/folio: .venv/Scripts/python -m tests.preview_server
"""

import io
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from PIL import Image

os.environ["MODULE_PREFIX"] = ""
os.environ["FOLIO_ADMIN_USER_ID"] = "preview"

from app import db, media, scenarios, security, worker
from app.main import app

fixture = tempfile.TemporaryDirectory(prefix="folio-ui-")
db.DATA = Path(fixture.name)
db.initialize()
with db.transaction() as con:
    preview_graph = scenarios.initial_graph("collage")
    for node in preview_graph["nodes"]:
        if node["kind"] in scenarios.TEXT_KINDS:
            node["text"] = {
                "photos": "Пришлите фотографии для коллажа. Когда закончите, нажмите «Завершить загрузку фото».",
                "details": "Какую надпись добавить на коллаж?",
            }.get(node["id"], "Продолжим.")
        if node["kind"] == "ask_photo":
            node.update(min=1, max=8, accept="готово")
    con.execute(
        "INSERT INTO accounts(id,name,client_id,secret) VALUES(1,'Тестовый кабинет','test',?)",
        (security.cipher().encrypt(b"not-a-real-key").decode(),),
    )
    con.execute(
        "INSERT INTO scenarios(name,product_type,draft) VALUES('Коллаж — черновик','collage',?)",
        (db.dump(preview_graph),),
    )
    con.execute("INSERT INTO users(id,name,role) VALUES('preview-manager','Менеджер','manager')")
    con.execute("UPDATE settings SET value=?", (db.dump({
        "send_enabled": True, "manager_scope": "all",
        "rework_prompts": {"collage": "Доработай коллаж: {correction}. Сохрани композицию."},
    }),))
    con.execute(
        "INSERT INTO versions(id,scenario_id,number,graph,author,created_at) VALUES(1,1,1,?,'preview',?)",
        (db.dump(preview_graph), db.now()),
    )
    con.execute("UPDATE scenarios SET published=1 WHERE id=1")
    con.execute("INSERT INTO mappings(id,account_id,sku,product_type,scenario_id) VALUES(1,1,'TEST_40x60','collage',1)")
    for index in range(1, 19):
        posting = f"85433468-{index:04}-1"
        worker.import_order(con, 1, {
            "posting_number": posting, "order_number": f"85433468-{index:04}",
            "status": "awaiting_packaging", "customer": {"name": f"Покупатель {index}"},
            "products": [{"sku": str(index), "offer_id": "TEST_40x60", "quantity": 1,
                          "name": "Коллаж на холсте 40×60, персональный макет по фотографиям"}],
        })
        item_id = con.execute("SELECT id FROM items WHERE posting=?", (posting,)).fetchone()[0]
        chat = worker.import_chat(con, 1, {"chat_id": f"fixture-chat-{index}",
                                         "chat_type": "BUYER_SELLER"}, posting=posting)
        job_id = con.execute(
            "INSERT INTO jobs(kind,account_id,payload,state,created_at) VALUES('start_chat',1,?,'done',?)",
            (db.dump({"posting": posting}), db.now()),
        ).lastrowid
        con.execute("INSERT INTO chat_items VALUES(?,?)", (chat["id"], item_id))
        con.execute("UPDATE chats SET active_item=?,start_job_id=?,buyer_name=?,unread=1 WHERE id=?",
                    (item_id, job_id, "Анна Иванова" if index == 1 else f"Покупатель {index}", chat["id"]))
        con.execute(
            "INSERT INTO instances(item_id,chat_id,version_id,node,fields,status) VALUES(?,?,1,'end',?,'closed')",
            (item_id, chat["id"], db.dump({"approval_outcome": "Покупатель отказался от макета"})),
        )
        for number, (actor, text) in enumerate([
            ("bot", "Здравствуйте! Пришлите фотографии и пожелания для коллажа."),
            ("buyer", "Добрый день! Хочу светлый фон и надпись снизу."),
            ("bot", "Спасибо, фотографии получены. Подготовим макет."),
            ("system", "Макет для согласования"),
            ("buyer", "Пожалуйста, сделайте фон теплее, надпись крупнее. Остальное оставьте."),
        ]):
            stamp = (datetime.now(timezone.utc) - timedelta(minutes=5-number)).isoformat()
            con.execute("INSERT INTO messages(chat_id,external_id,actor,body,created_at) VALUES(?,?,?,?,?)",
                        (chat["id"], f"fixture-{index}-{number}", actor, text, stamp))
        if index == 1:
            result = io.BytesIO()
            Image.new("RGB", (1000, 1000), "beige").save(result, "JPEG")
            preview_id = media.store(con, result.getvalue(), db.config(con), chat["id"], item_id)
            con.execute("UPDATE messages SET media_ids=? WHERE external_id='fixture-1-3'", (db.dump([preview_id]),))
            for revision in range(3):
                con.execute(
                    "INSERT INTO image_jobs(item_id,media_id,prompt,width_cm,height_cm,state,preview_media_id,"
                    "created_at,updated_at,node_id,chat_id) VALUES(?,?,'Доработка',40,60,'completed',?,?,?,?,?)",
                    (item_id, preview_id, preview_id, db.now(), db.now(), f"rework:fixture-{revision}", chat["id"]),
                )
    con.execute(
        "INSERT INTO jobs(kind,account_id,state,error,created_at,finished_at) "
        "VALUES('probe',1,'failed','legacy_probe_configuration',?,?)",
        (db.now(), db.now()),
    )
    db.audit(con, "preview", "scenario.created", "scenario", 1)


@app.middleware("http")
async def fixture_identity(request, call_next):
    request.scope["headers"] = [
        (b"x-bellenne-user-id", b"preview" if request.url.path.startswith("/settings") else b"preview-manager"),
        (b"x-bellenne-csrf-token", b"preview-csrf"),
    ] + request.scope["headers"]
    return await call_next(request)


if __name__ == "__main__":
    try:
        uvicorn.run(app, host="127.0.0.1", port=18743)
    finally:
        fixture.cleanup()
