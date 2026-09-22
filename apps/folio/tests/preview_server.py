"""Local, synthetic UI fixture. No worker, no real keys, no production auth bypass.

Run from apps/folio: .venv/Scripts/python -m tests.preview_server
"""

import os
import tempfile
from pathlib import Path

import uvicorn

os.environ["MODULE_PREFIX"] = ""
os.environ["FOLIO_ADMIN_USER_ID"] = "preview"

from app import db, scenarios, security
from app.main import app

fixture = tempfile.TemporaryDirectory(prefix="folio-ui-")
db.DATA = Path(fixture.name)
db.initialize()
with db.transaction() as con:
    con.execute(
        "INSERT INTO accounts(id,name,client_id,secret) VALUES(1,'Тестовый кабинет','test',?)",
        (security.cipher().encrypt(b"not-a-real-key").decode(),),
    )
    con.execute(
        "INSERT INTO chats(id,account_id,external_id,title,updated_at) VALUES(1,1,'test-chat','Тестовый чат · Картина',?)",
        (db.now(),),
    )
    con.execute(
        "INSERT INTO messages(chat_id,external_id,actor,body,created_at) VALUES(1,'test-message','buyer','Тестовое сообщение для проверки интерфейса',?)",
        (db.now(),),
    )
    con.execute(
        "INSERT INTO scenarios(name,product_type,draft) VALUES('Коллаж — черновик','collage',?)",
        (db.dump(scenarios.initial_graph("collage")),),
    )


@app.middleware("http")
async def fixture_identity(request, call_next):
    request.scope["headers"] = [
        (b"x-bellenne-user-id", b"preview"),
        (b"x-bellenne-csrf-token", b"preview-csrf"),
    ] + request.scope["headers"]
    return await call_next(request)


if __name__ == "__main__":
    try:
        uvicorn.run(app, host="127.0.0.1", port=18743)
    finally:
        fixture.cleanup()
