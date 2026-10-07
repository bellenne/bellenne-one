import io
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from app import db, image_tasks, mattermost, media, retailcrm, scenarios, security, worker
from app.main import app
from app.ozon import OzonAdapter, OzonError
from app.simulation import simulate


def graph(kind):
    result = scenarios.initial_graph(kind)
    for node in result["nodes"]:
        if node["kind"] in scenarios.TEXT_KINDS:
            node["text"] = "Approved prompt " + node["id"]
        if node["kind"] == "ask_photo":
            node.update(min=1, max=8 if kind == "collage" else 1)
            node["accept"] = "photos_done"
        if node["kind"] == "confirm":
            node["accept"] = "yes"
    return result


class FolioTest(unittest.TestCase):
    @contextmanager
    def image_worker_settings(self, values):
        response = self.client.post(
            "/settings/image-worker", headers=self.admin,
            data={
                "csrf": "csrf",
                "base_url": values["FOLIO_IMAGE_WORKER_URL"],
                "api_key": values["FOLIO_IMAGE_WORKER_API_KEY"],
            }, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        yield

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = db.DATA
        db.DATA = Path(self.temp.name)
        self.env = patch.dict(
            os.environ,
            {
                "FOLIO_ADMIN_USER_ID": "1",
                "FOLIO_PUBLIC_BASE_URL": "https://one.example.ru",
            },
        )
        self.env.start()
        db.initialize()
        self.settings = {
            "handoff_hours": 0,
            "timer_origin": "confirmation",
            "manager_scope": "assigned",
            "manager_resume": True,
            "send_enabled": True,
        }
        with db.transaction() as con:
            con.execute("UPDATE settings SET value=?", (db.dump(self.settings),))
            con.execute("INSERT INTO users(id,name,role) VALUES('2','Manager','manager')")
            con.execute(
                "INSERT INTO accounts(id,name,client_id,secret,capabilities) VALUES(1,'Test','client',?,'{\"history\":true}')",
                (security.cipher().encrypt(b"PRIVATE_TEST_KEY").decode(),),
            )
        self.admin = {"X-Bellenne-User-Id": "1", "X-Bellenne-Csrf-Token": "csrf"}
        self.manager = {**self.admin, "X-Bellenne-User-Id": "2"}
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.env.stop()
        db.DATA = self.previous
        self.temp.cleanup()

    def test_local_manager_login_is_limited_to_folio_role(self):
        created = self.client.post(
            "/users/local", headers=self.admin,
            data={"csrf": "csrf", "username": "designer", "name": "Дизайнер",
                  "password": "StrongLocalPass123"},
            follow_redirects=False,
        )
        self.assertEqual(created.status_code, 303)
        bad = self.client.post("/login", data={"username": "designer", "password": "wrong"})
        self.assertEqual(bad.status_code, 401)
        login = self.client.post(
            "/login", data={"username": "designer", "password": "StrongLocalPass123"},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 303)
        token = login.cookies[security.COOKIE_NAME]
        authenticated = self.client.get("/auth/check", cookies={security.COOKIE_NAME: token})
        self.assertEqual(authenticated.status_code, 204)
        self.assertEqual(authenticated.headers["X-Bellenne-User-Id"], "folio:designer")
        headers = {"X-Bellenne-User-Id": "folio:designer",
                   "X-Bellenne-Csrf-Token": authenticated.headers["X-Bellenne-Csrf-Token"]}
        self.assertEqual(self.client.get("/", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/settings", headers=headers).status_code, 403)
        with db.transaction() as con:
            con.execute("INSERT OR IGNORE INTO revoked_users VALUES('folio:designer')")
        self.assertEqual(self.client.get("/auth/check", cookies={security.COOKIE_NAME: token}).status_code, 401)

    def setup_instance(
        self, kind="portrait_background", posting="ORDER", external_chat="CHAT"
    ):
        with db.transaction() as con:
            sid = con.execute(
                "INSERT INTO scenarios(name,product_type,draft) VALUES('Scenario',?,?)",
                (kind, db.dump(graph(kind))),
            ).lastrowid
            scenarios.publish(con, sid, "1")
            con.execute("INSERT OR IGNORE INTO templates(id,name) VALUES(1,'Template')")
            offer = "offer" + str(sid)
            sku = str(sid)
            con.execute(
                "INSERT INTO mappings(account_id,sku,product_type,scenario_id,template_ids) VALUES(1,?,?,?,'[1]')",
                (offer, kind, sid),
            )
            worker.import_order(
                con,
                1,
                {
                    "posting_number": posting,
                    "status": "awaiting_packaging",
                    "products": [
                        {
                            "sku": sku,
                            "offer_id": offer,
                            "name": "Painting",
                            "quantity": 1,
                        }
                    ],
                },
            )
            item = con.execute(
                "SELECT id FROM items WHERE offer_id=?", (offer,)
            ).fetchone()[0]
            chat = worker.import_chat(con, 1, {"chat_id": external_chat}, posting=posting)
            start_job_id = con.execute(
                "INSERT INTO jobs(kind,account_id,payload,state,created_at) "
                "VALUES('start_chat',1,?,'done',?)",
                (db.dump({"posting": posting}), db.now()),
            ).lastrowid
            con.execute("INSERT INTO chat_items VALUES(?,?)", (chat["id"], item))
            con.execute(
                "UPDATE chats SET mode='bot',active_item=?,start_job_id=? WHERE id=?",
                (item, start_job_id, chat["id"]),
            )
            scenarios.start(con, item, chat["id"])
            instance = con.execute(
                "SELECT id FROM instances WHERE item_id=?", (item,)
            ).fetchone()[0]
            scenarios.advance(con, instance)
        return instance, item, chat["id"], sid

    def image(self, con, chat, item):
        data = io.BytesIO()
        Image.new("RGB", (2, 2)).save(data, format="PNG")
        return media.store(con, data.getvalue(), self.settings, chat, item)

    def finish(self, kind):
        iid, item, chat, sid = self.setup_instance(kind)
        with db.transaction() as con:
            mid = self.image(con, chat, item)
            scenarios.advance(con, iid, {"media_ids": [mid]})
            scenarios.advance(
                con, iid, {"text": "1" if kind == "template_art" else "Details"}
            )
            scenarios.advance(con, iid, {"text": "yes"})
            con.execute("UPDATE outbox SET state='sent'")
        return iid, item, chat, sid

    def test_three_product_types_and_delayed_readiness(self):
        for kind in scenarios.TYPES:
            with self.subTest(kind=kind):
                answers = (
                    ["photo:1"]
                    + (["photos_done"] if kind == "collage" else [])
                    + ["1" if kind == "template_art" else "Details", "yes"]
                )
                result = simulate(graph(kind), kind, answers)
                self.assertEqual(result["instance"]["status"], "needs_review")
        iid, *_ = self.finish("portrait_background")
        with db.transaction() as con:
            scenarios.approve(con, iid, "2")
            scenarios.release_due(con)
            self.assertEqual(
                con.execute("SELECT status FROM instances").fetchone()[0], "ready"
            )

    def test_collage_eight_and_nine_photos(self):
        self.assertEqual(
            simulate(graph("collage"), "collage", ["photo:8", "Text", "yes"])[
                "instance"
            ]["status"],
            "needs_review",
        )
        result = simulate(graph("collage"), "collage", ["photo:9"])
        self.assertEqual(result["instance"]["status"], "needs_manager")
        self.assertNotIn("photos", json.loads(result["instance"]["fields"]))

    def test_template_requires_single_allowed_choice(self):
        for answer in ("", "1 2", "999"):
            result = simulate(
                graph("template_art"), "template_art", ["photo:1", answer, "yes"]
            )
            self.assertEqual(result["instance"]["status"], "needs_manager")

    def test_bad_graphs_and_unsafe_variables(self):
        for mutation in (
            lambda g: g["nodes"][0].update(next="missing"),
            lambda g: g["nodes"][1].update(next="start"),
            lambda g: g["nodes"][1].update(text="{posting.__class__}"),
            lambda g: g["nodes"][1].update(max=9),
        ):
            value = graph("collage")
            mutation(value)
            with self.assertRaises(scenarios.Invalid):
                scenarios.validate(value, "collage")

    def test_condition_branches_on_an_answer_collected_before_it(self):
        value = {
            "nodes": [
                {"id": "start", "kind": "start", "next": "agreement"},
                {
                    "id": "agreement",
                    "title": "Спросить про согласование",
                    "kind": "ask_text",
                    "field": "wishes",
                    "required": False,
                    "text": "Нужно ли согласование?",
                    "next": "approval_condition",
                },
                {
                    "id": "approval_condition",
                    "title": "Проверить согласование",
                    "kind": "condition",
                    "field": "wishes",
                    "equals": "БЕЗ СОГЛАСОВАНИЯ",
                    "next": "photos",
                    "otherwise": "background",
                },
                {
                    "id": "background",
                    "kind": "ask_text",
                    "field": "background",
                    "required": False,
                    "text": "Какой фон вы хотите?",
                    "next": "photos",
                },
                {
                    "id": "photos",
                    "kind": "ask_photo",
                    "field": "photos",
                    "required": True,
                    "text": "Пришлите фотографию",
                    "min": 1,
                    "max": 1,
                    "next": "message",
                },
                {
                    "id": "message",
                    "kind": "send",
                    "text": "Спасибо, данные получены",
                    "next": "ready",
                },
                {"id": "ready", "kind": "ready"},
            ]
        }
        scenarios.validate(value, "portrait_background")
        skipped = simulate(
            value,
            "portrait_background",
            ["БЕЗ СОГЛАСОВАНИЯ", "photo:1"],
        )
        self.assertEqual(skipped["instance"]["status"], "needs_review")
        self.assertNotIn("background", json.loads(skipped["instance"]["fields"]))

        requested = simulate(
            value,
            "portrait_background",
            ["НУЖНО СОГЛАСОВАНИЕ", "Светлый фон", "photo:1"],
        )
        self.assertEqual(requested["instance"]["status"], "needs_review")
        self.assertEqual(
            json.loads(requested["instance"]["fields"])["background"],
            "Светлый фон",
        )

    def test_first_reply_can_be_photo_or_text_without_manual_takeover(self):
        value = {
            "nodes": [
                {"id": "start", "kind": "start", "next": "answer"},
                {"id": "answer", "kind": "ask_input", "field": "wishes",
                 "required": True, "text": "Пришлите пожелания или сразу фото",
                 "min": 1, "max": 1, "next": "check", "otherwise": "accepted"},
                {"id": "check", "kind": "condition", "field": "wishes",
                 "equals": "БЕЗ СОГЛАСОВАНИЯ", "next": "photos",
                 "otherwise": "background"},
                {"id": "background", "kind": "ask_text", "field": "background",
                 "text": "Опишите фон", "next": "photos"},
                {"id": "photos", "kind": "ask_photo", "field": "photos",
                 "required": True, "text": "Пришлите фото", "min": 1,
                 "max": 1, "next": "accepted"},
                {"id": "accepted", "kind": "send", "text": "Фото принято",
                 "next": "ready"},
                {"id": "ready", "kind": "ready"},
            ]
        }
        scenarios.validate(value, "portrait_background")
        direct = simulate(value, "portrait_background", ["photo:1"])
        self.assertEqual(direct["instance"]["status"], "needs_review")
        self.assertEqual(len(json.loads(direct["instance"]["fields"])["photos"]), 1)
        self.assertEqual(direct["timeline"][-1]["body"], "Фото принято")
        wishes = simulate(value, "portrait_background", ["Нужен фон", "Тёмный", "photo:1"])
        self.assertEqual(wishes["instance"]["status"], "needs_review")
        self.assertEqual(json.loads(wishes["instance"]["fields"])["background"], "Тёмный")

    def test_mockup_approval_branches_and_retailcrm_note_in_dry_run(self):
        value = {"nodes": [
            {"id": "start", "kind": "start", "next": "photo"},
            {"id": "photo", "kind": "ask_photo", "field": "photos", "required": True,
             "text": "Пришлите фото", "min": 1, "max": 1, "next": "deal"},
            {"id": "deal", "kind": "retailcrm", "comment": "Заказ {posting}",
             "next": "mockup", "error": "handoff"},
            {"id": "mockup", "kind": "await_mockup", "next": "approval"},
            {"id": "approval", "kind": "approval", "text": "Подтвердите за 6 часов",
             "accept": "ДА", "reject": "НЕТ", "hours": 6,
             "next": "note", "error": "handoff"},
            {"id": "note", "kind": "retailcrm_note",
             "comment": "Итог: {approval_outcome}. Отправление {posting}.",
             "next": "handoff", "error": "handoff"},
            {"id": "handoff", "kind": "handoff", "next": "end"},
            {"id": "end", "kind": "end"},
        ]}
        scenarios.validate(value, "portrait_background")
        for answer, expected in [
            ({"kind": "text", "value": "ДА"}, "Макет подтверждён покупателем"),
            ({"kind": "text", "value": "Да, макет подходит!"}, "Макет подтверждён покупателем"),
            ({"kind": "text", "value": "НЕТ"}, "Покупатель отказался от макета"),
            ({"kind": "text", "value": "Нет, нужны правки."}, "Покупатель отказался от макета"),
            ({"kind": "approval_timeout"}, "Нет ответа за 6 ч."),
        ]:
            result = simulate(value, "portrait_background", [
                {"kind": "photo", "count": 1}, {"kind": "manager_photo"}, answer,
            ])
            self.assertEqual(result["instance"]["status"], "needs_manager")
            self.assertEqual(json.loads(result["instance"]["fields"])["approval_outcome"], expected)
            self.assertIn("Подтвердите за 6 часов", [m["body"] for m in result["timeline"]])
        ambiguous = simulate(value, "portrait_background", [
            {"kind": "photo", "count": 1}, {"kind": "manager_photo"},
            {"kind": "text", "value": "Да, но переделайте фон"},
        ])
        self.assertEqual(ambiguous["instance"]["status"], "needs_manager")
        self.assertNotIn("approval_outcome", json.loads(ambiguous["instance"]["fields"]))

    def test_manager_image_is_sent_as_file_then_approval_timer_starts(self):
        instance_id, item_id, chat_id, _ = self.setup_instance()
        mockup_graph = {"nodes": [
            {"id": "start", "kind": "start", "next": "mockup"},
            {"id": "mockup", "kind": "await_mockup", "next": "approval"},
            {"id": "approval", "kind": "approval", "text": "Ответьте за 6 часов",
             "accept": "ДА", "reject": "НЕТ", "hours": 6,
             "next": "note", "error": "handoff"},
            {"id": "note", "kind": "retailcrm_note", "comment": "{approval_outcome}",
             "next": "handoff", "error": "handoff"},
            {"id": "handoff", "kind": "handoff", "next": "end"},
            {"id": "end", "kind": "end"},
        ]}
        with db.transaction() as con:
            version = con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0]
            con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump(mockup_graph), version))
            con.execute("UPDATE instances SET node='mockup',status='waiting_mockup',prompted=0 WHERE id=?", (instance_id,))
            con.execute("DELETE FROM outbox WHERE chat_id=?", (chat_id,))
        photo = io.BytesIO()
        Image.new("RGB", (2, 2)).save(photo, format="PNG")
        response = self.client.post(
            f"/chats/{chat_id}/send_file", headers=self.admin,
            data={"csrf": "csrf", "dedup": "file-1"},
            files={"file": ("mockup.png", photo.getvalue(), "image/png")},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with db.transaction() as con:
            queued = con.execute("SELECT * FROM outbox WHERE chat_id=?", (chat_id,)).fetchone()
            self.assertEqual(queued["kind"], "file")
            self.assertEqual(queued["instance_id"], instance_id)
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "bot")

        sent_calls = []
        class Adapter:
            def __init__(self, _account, _settings):
                pass
            def send_file(self, _chat, filename, content):
                sent_calls.append((filename, content))
                return "ozon-file-1"
            def send(self, _chat, text):
                sent_calls.append(text)
                return "ozon-text-1"
            def close(self):
                pass

        self.assertTrue(worker.send_one(Adapter))
        self.assertEqual(sent_calls[0][0], "folio-image.png")
        self.assertEqual(sent_calls[0][1], photo.getvalue())
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT status FROM instances WHERE id=?", (instance_id,)).fetchone()[0], "waiting_approval")
            self.assertIsNone(con.execute("SELECT approval_due_at FROM instances WHERE id=?", (instance_id,)).fetchone()[0])
        self.assertTrue(worker.send_one(Adapter))
        with db.transaction() as con:
            self.assertIsNotNone(con.execute("SELECT approval_due_at FROM instances WHERE id=?", (instance_id,)).fetchone()[0])
            scenarios.advance(con, instance_id, {"text": "ДА"})
            action = con.execute("SELECT * FROM retailcrm_actions WHERE instance_id=?", (instance_id,)).fetchone()
            self.assertEqual(action["kind"], "note")
            self.assertEqual(con.execute("SELECT kind FROM jobs WHERE payload LIKE ?", (f'%"action_id":{action["id"]}%',)).fetchone()[0], "retailcrm_note")

    def test_chat_and_settings_explain_missing_photo_base_after_retail_failure(self):
        instance_id, _item_id, chat_id, _ = self.setup_instance()
        with db.transaction() as con:
            con.execute(
                "INSERT INTO retailcrm_actions(instance_id,node_id,state,external_id,error,created_at,kind) "
                "VALUES(?,?,'failed','test','retailcrm_public_base_url_missing',?,'create')",
                (instance_id, "retail", db.now()),
            )
            con.execute("UPDATE instances SET status='closed' WHERE id=?", (instance_id,))
        chat = self.client.get(f"/chats/{chat_id}", headers=self.admin)
        self.assertEqual(chat.status_code, 200)
        self.assertIn("Адрес Folio для ссылок на фото не настроен", chat.text)
        self.assertIn("Отправка фото переведёт чат в ручной режим", chat.text)
        with patch.dict(os.environ, {"FOLIO_PUBLIC_BASE_URL": ""}):
            settings = self.client.get("/settings/retailcrm", headers=self.admin)
        self.assertEqual(settings.status_code, 200)
        self.assertIn("FOLIO_PUBLIC_BASE_URL", settings.text)

    def test_retailcrm_approval_note_preserves_customer_comment_and_items(self):
        instance_id, _item_id, chat_id, _ = self.setup_instance()
        value = {"nodes": [
            {"id": "start", "kind": "start", "next": "note"},
            {"id": "note", "kind": "retailcrm_note", "comment": "Итог: {approval_outcome}",
             "next": "handoff", "error": "handoff"},
            {"id": "handoff", "kind": "handoff", "next": "end"},
            {"id": "end", "kind": "end"},
        ]}
        with db.transaction() as con:
            version = con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0]
            con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump(value), version))
            con.execute("UPDATE instances SET node='note',status='waiting_integration',fields=? WHERE id=?",
                        (db.dump({"approval_outcome": "Макет подтверждён покупателем"}), instance_id))
            con.execute("INSERT INTO retailcrm_integrations(id,base_url,site,secret,capabilities,checked_at) VALUES(1,?,?,?,?,?)",
                        ("https://shop.retailcrm.ru", "shop", security.cipher().encrypt(b"KEY").decode(),
                         db.dump({"order_read": True, "order_write": True, "site": True}), db.now()))
            action_id = con.execute(
                "INSERT INTO retailcrm_actions(instance_id,node_id,external_id,created_at,kind) VALUES(?,?,?,?,?)",
                (instance_id, "note", retailcrm.external_id(instance_id), db.now(), "note"),
            ).lastrowid
            db.job(con, "retailcrm_note", 1, {"action_id": action_id})
            job = con.execute("SELECT * FROM jobs WHERE kind='retailcrm_note' ORDER BY id DESC LIMIT 1").fetchone()
        writes = []
        class FakeRetail:
            def __init__(self, _record):
                pass
            def find_order(self, _external_id):
                return {"order": {"id": 7, "customerComment": "Старый комментарий",
                                  "managerComment": "Не изменять",
                                  "items": [{"id": 11}, {"id": 12}]}}
            def edit_order_customer_comment(self, external_id, comment, items):
                writes.append((external_id, comment, items))
                return {"success": True}
            def close(self):
                pass
        state, error = worker.retailcrm_note_job(job, FakeRetail)
        self.assertEqual((state, error), ("sent", None))
        self.assertEqual(writes[0][0], retailcrm.external_id(instance_id))
        self.assertIn("Старый комментарий\n\nИтог: Макет подтверждён покупателем", writes[0][1])
        self.assertEqual(writes[0][2], [{"id": 11}, {"id": 12}])
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT status FROM instances WHERE id=?", (instance_id,)).fetchone()[0], "needs_manager")
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "manual")

    def test_condition_reports_missing_prior_answer_separately_from_otherwise(self):
        value = graph("portrait_background")
        value["nodes"].insert(
            1,
            {
                "id": "condition",
                "title": "Проверить условие",
                "kind": "condition",
                "field": "wishes",
                "equals": "БЕЗ СОГЛАСОВАНИЯ",
                "next": "photos",
                "otherwise": "photos",
            },
        )
        value["nodes"][0]["next"] = "condition"
        with self.assertRaisesRegex(scenarios.Invalid, "до него .* ещё не собраны"):
            scenarios.validate(value, "portrait_background")

        value["nodes"][1]["otherwise"] = ""
        with self.assertRaisesRegex(scenarios.Invalid, "Если условие не выполнено"):
            scenarios.validate(value, "portrait_background")

    def test_version_pinning(self):
        iid, _item, _chat, sid = self.setup_instance()
        with db.transaction() as con:
            old = con.execute(
                "SELECT version_id FROM instances WHERE id=?", (iid,)
            ).fetchone()[0]
            value = graph("portrait_background")
            value["nodes"][1]["text"] = "New approved prompt"
            con.execute(
                "UPDATE scenarios SET draft=? WHERE id=?", (db.dump(value), sid)
            )
            new = scenarios.publish(con, sid, "1")
            self.assertNotEqual(old, new)
            self.assertEqual(
                con.execute(
                    "SELECT version_id FROM instances WHERE id=?", (iid,)
                ).fetchone()[0],
                old,
            )

    def test_order_dedup_preserves_new_order(self):
        payload = {
            "posting_number": "A",
            "status": "created",
            "products": [
                {
                    "sku": "100",
                    "offer_id": "unknown",
                    "name": "portrait_background",
                    "quantity": 1,
                }
            ],
        }
        with db.transaction() as con:
            worker.import_order(con, 1, payload)
            worker.import_order(con, 1, payload)
            payload["posting_number"] = "B"
            worker.import_order(con, 1, payload)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM items WHERE mapping_id IS NOT NULL"
                ).fetchone()[0],
                0,
            )
            with self.assertRaises(OzonError):
                worker.import_order(con, 1, {"posting_number": "C"})

    def test_chat_uses_buyer_and_separates_order_from_posting_number(self):
        with db.transaction() as con:
            worker.import_order(
                con,
                1,
                {
                    "posting_number": "76076007-0252-1",
                    "order_number": "ii50069115267",
                    "status": "awaiting_packaging",
                    "customer": {"id": "buyer-7", "name": "Анна Иванова"},
                    "products": [
                        {
                            "product_id": "987654321",
                            "product_offer_id": "folio-portrait",
                            "product_name": "Портрет по фото",
                            "quantity": 1,
                        }
                    ],
                },
            )
            chat = worker.import_chat(con, 1, {"chat_id": "technical-chat-id"})
            worker.import_message(
                con,
                chat,
                {
                    "message_id": "3000000000817031942",
                    "context": {
                        "order_number": "ii50069115267",
                        "sku": "987654321",
                    },
                    "user": {
                        "id": "buyer-7",
                        "name": "Анна Иванова",
                        "type": "Сustomer",
                    },
                    "data": ["Здравствуйте"],
                },
            )
            stored = con.execute("SELECT * FROM chats").fetchone()
            item = con.execute("SELECT * FROM items").fetchone()
            message = con.execute("SELECT * FROM messages").fetchone()
            self.assertEqual(stored["buyer_name"], "Анна Иванова")
            self.assertEqual(item["posting"], "76076007-0252-1")
            self.assertEqual(item["order_number"], "ii50069115267")
            self.assertEqual(message["actor"], "buyer")
            self.assertEqual(message["order_number"], "ii50069115267")
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM chat_items").fetchone()[0], 1
            )
        page = self.client.get("/", headers=self.admin)
        self.assertNotIn("Анна Иванова", page.text)
        self.assertEqual(
            self.client.get(f"/chats/{chat['id']}", headers=self.admin).status_code,
            404,
        )
        self.assertNotIn("<strong>technical-chat-id</strong>", page.text)

    def test_chat_order_context_fetches_exact_posting_and_manual_buyer_name(self):
        with db.transaction() as con:
            chat = worker.import_chat(
                con,
                1,
                {"chat_id": "buyer-chat", "chat_type": "BUYER_SELLER"},
            )
            worker.import_message(
                con,
                chat,
                {
                    "message_id": "message-1",
                    "created_at": "2026-09-23T07:00:00Z",
                    "user": {"id": "115568", "type": "Customer"},
                    "context": {"order_number": "OZON-ORDER", "sku": "987"},
                    "data": ["Здравствуйте"],
                },
            )

        calls = []

        class Adapter:
            def order_pages(self, since, until, cursor="", order_numbers=None):
                calls.append(tuple(order_numbers))
                yield [
                    {
                        "posting_number": "76076007-0252-1",
                        "order_number": "OZON-ORDER",
                        "status": "awaiting_packaging",
                        "products": [
                            {
                                "sku": 987,
                                "offer_id": "PORTRAIT",
                                "name": "Портрет",
                                "quantity": 1,
                            }
                        ],
                    }
                ], None

        worker.backfill_chat_orders(1, Adapter())
        worker.backfill_chat_orders(1, Adapter())
        self.assertEqual(calls, [("OZON-ORDER",)])
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM chat_items").fetchone()[0], 1)
            self.assertIsNotNone(
                con.execute("SELECT active_item FROM chats WHERE id=?", (chat["id"],))
                .fetchone()[0]
            )
        page = self.client.get("/", headers=self.admin)
        self.assertNotIn("Покупатель 115568", page.text)

        response = self.client.post(
            f"/chats/{chat['id']}/buyer_name",
            headers=self.admin,
            data={"csrf": "csrf", "buyer_name": "Анна Иванова"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 404)

    def test_admin_can_link_chat_by_known_posting_when_ozon_omits_context(self):
        with db.transaction() as con:
            worker.import_order(
                con,
                1,
                {
                    "posting_number": "POSTING-1",
                    "order_number": "ORDER-1",
                    "products": [
                        {"sku": "SKU-1", "offer_id": "OFFER-1", "quantity": 1}
                    ],
                },
            )
            chat = worker.import_chat(
                con,
                1,
                {"chat_id": "missing-context", "chat_type": "BUYER_SELLER"},
            )
        data = {
            "csrf": "csrf",
            "chat_id": str(chat["id"]),
            "posting_number": "POSTING-1",
        }
        self.assertEqual(
            self.client.post("/links", headers=self.manager, data=data).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                "/links", headers=self.admin, data=data, follow_redirects=False
            ).status_code,
            404,
        )
        with db.transaction() as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM chat_items").fetchone()[0], 0
            )
        page = self.client.get(f"/chats/{chat['id']}", headers=self.admin)
        self.assertEqual(page.status_code, 404)

    def test_sync_ignores_support_chats_and_keeps_buyer_identifier(self):
        calls = []

        class Adapter:
            def chats(self):
                return iter(
                    [
                        {
                            "chat": {
                                "chat_id": "support-chat",
                                "chat_type": "SELLER_SUPPORT",
                            }
                        },
                        {
                            "chat": {
                                "chat_id": "buyer-chat",
                                "chat_type": "BUYER_SELLER",
                            }
                        },
                    ]
                )

            def history(self, chat_id):
                calls.append(chat_id)
                return iter(
                    [
                        {
                            "message_id": "buyer-message",
                            "user": {"id": "115568", "type": "Сustomer"},
                            "data": ["Здравствуйте"],
                        }
                    ]
                )

        worker.sync_job(
            {"kind": "chat_sync", "account_id": 1, "payload": "{}"},
            Adapter(),
            self.settings,
        )
        self.assertEqual(calls, [])
        with db.transaction() as con:
            chats = con.execute(
                "SELECT external_id,chat_type,buyer_id FROM chats"
            ).fetchall()
            self.assertEqual(len(chats), 0)
        page = self.client.get("/", headers=self.admin)
        self.assertNotIn("Покупатель 115568", page.text)
        self.assertNotIn("support-chat", page.text)

    def test_unidentified_empty_legacy_chats_are_hidden_from_inbox(self):
        with db.transaction() as con:
            worker.import_chat(con, 1, {"chat_id": "legacy-empty-chat"})
        page = self.client.get("/", headers=self.admin)
        self.assertNotIn("legacy-empty-chat", page.text)
        self.assertNotIn("<strong>Покупатель</strong>", page.text)
        with db.transaction() as con:
            buyer_chat = worker.import_chat(
                con,
                1,
                {"chat_id": "empty-buyer-chat", "chat_type": "BUYER_SELLER"},
            )
        page = self.client.get("/", headers=self.admin)
        self.assertNotIn(f"Диалог №{buyer_chat['id']}", page.text)
        self.assertEqual(
            self.client.get(f"/chats/{buyer_chat['id']}", headers=self.admin).status_code,
            404,
        )

    def test_order_sync_starts_only_mapped_seller_article_and_own_chat(self):
        with db.transaction() as con:
            sid = con.execute(
                "INSERT INTO scenarios(name,product_type,draft) VALUES('Portrait','portrait_background',?)",
                (db.dump(graph("portrait_background")),),
            ).lastrowid
            scenarios.publish(con, sid, "1")
            con.execute(
                "INSERT INTO mappings(account_id,sku,product_type,scenario_id,key_kind) "
                "VALUES(1,'KARTINA-1-40-60','portrait_background',?,'seller_article')",
                (sid,),
            )
            con.execute(
                "UPDATE accounts SET capabilities=?,checked_at=?,error=NULL WHERE id=1",
                (db.dump({"account": True, "orders": True, "list": True}), db.now()),
            )
            legacy = worker.import_chat(
                con, 1, {"chat_id": "unrelated-buyer", "chat_type": "BUYER_SELLER"}
            )
        settings = {
            **self.settings,
            "sync_since": "2026-09-01T00:00:00Z",
            "start_enabled": True,
            "automation_enabled": True,
            "automation_enabled_at": "2026-09-23T00:00:00+00:00",
        }
        with db.transaction() as con:
            con.execute("UPDATE settings SET value=? WHERE id=1", (db.dump(settings),))

        class Adapter:
            def __init__(self):
                self.starts = []
                self.histories = []

            def seller_info(self):
                return {}

            def order_pages(self, *_args):
                yield [
                    {
                        "posting_number": "POSTING-1",
                        "order_number": "ORDER-1",
                        "in_process_at": "2026-09-24T08:00:00Z",
                        "status": "created",
                        "products": [
                            {"sku": "1001", "offer_id": "KARTINA-1-40-60", "quantity": 1},
                            {"sku": "1002", "offer_id": "OTHER", "quantity": 1},
                        ],
                    },
                    {
                        "posting_number": "POSTING-2",
                        "order_number": "ORDER-2",
                        "in_process_at": "2026-09-24T08:00:00Z",
                        "status": "awaiting_packaging",
                        "products": [
                            {"sku": "1003", "offer_id": "OTHER", "quantity": 1}
                        ],
                    },
                    {
                        "posting_number": "POSTING-OLD",
                        "order_number": "ORDER-OLD",
                        "in_process_at": "2026-09-22T08:00:00Z",
                        "status": "awaiting_packaging",
                        "products": [
                            {"sku": "1004", "offer_id": "KARTINA-1-40-60", "quantity": 1}
                        ],
                    },
                ], None

            def chats(self):
                return iter([
                    {"chat": {"chat_id": "unrelated-buyer", "chat_type": "BUYER_SELLER"}}
                ])

            def history(self, chat_id):
                self.histories.append(chat_id)
                return iter([])

            def start(self, posting):
                self.starts.append(posting)
                return "folio-new-chat"

        adapter = Adapter()
        worker.sync_job(
            {"kind": "sync", "account_id": 1, "payload": "{}"}, adapter, settings
        )
        with db.transaction() as con:
            jobs = con.execute(
                "SELECT * FROM jobs WHERE kind='start_chat'"
            ).fetchall()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(json.loads(jobs[0]["payload"]), {"posting": "POSTING-1"})
            managed_item = con.execute(
                "SELECT id FROM items WHERE offer_id='KARTINA-1-40-60'"
            ).fetchone()[0]
        self.assertEqual(adapter.histories, [])
        worker.sync_job(jobs[0], adapter, settings)
        self.assertEqual(adapter.starts, ["POSTING-1"])
        self.assertEqual(adapter.histories, [])
        sent = []

        class OutgoingAdapter:
            def __init__(self, *_args):
                pass

            def send(self, chat_id, body):
                sent.append((chat_id, body))
                return "first-message-id"

            def close(self):
                pass

        self.assertTrue(worker.send_one(OutgoingAdapter))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "folio-new-chat")
        self.assertIn("Approved prompt", sent[0][1])
        with db.transaction() as con:
            owned = con.execute(
                "SELECT * FROM chats WHERE external_id='folio-new-chat'"
            ).fetchone()
            self.assertEqual(owned["start_job_id"], jobs[0]["id"])
            self.assertEqual(owned["active_item"], managed_item)
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM chat_items WHERE chat_id=?", (owned["id"],)).fetchone()[0],
                1,
            )
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM instances WHERE item_id=?", (managed_item,)).fetchone()[0],
                1,
            )
        orders = self.client.get("/orders", headers=self.admin).text
        self.assertIn("POSTING-1", orders)
        self.assertNotIn("POSTING-2", orders)
        self.assertIn("POSTING-OLD", orders)
        self.assertIn("Заказ оформлен до включения автоматизации", orders)
        inbox = self.client.get("/", headers=self.admin).text
        self.assertIn("POSTING-1", inbox)
        self.assertNotIn("Номер заказа Ozon: ORDER-1", inbox)
        self.assertNotIn("unrelated-buyer", inbox)
        self.assertEqual(
            self.client.get(f"/chats/{legacy['id']}", headers=self.admin).status_code,
            404,
        )
        worker.sync_job(
            {"kind": "sync", "account_id": 1, "payload": "{}"}, adapter, settings
        )
        with db.transaction() as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM jobs WHERE kind='start_chat'").fetchone()[0],
                1,
            )

    def test_ambiguous_managed_posting_does_not_start_chat(self):
        with db.transaction() as con:
            sid = con.execute(
                "INSERT INTO scenarios(name,product_type,draft) VALUES('Portrait','portrait_background',?)",
                (db.dump(graph("portrait_background")),),
            ).lastrowid
            scenarios.publish(con, sid, "1")
            for article in ("KARTINA-1", "KARTINA-2"):
                con.execute(
                    "INSERT INTO mappings(account_id,sku,product_type,scenario_id,key_kind) "
                    "VALUES(1,?,'portrait_background',?,'seller_article')",
                    (article, sid),
                )
            con.execute(
                "UPDATE accounts SET capabilities=?,error=NULL WHERE id=1",
                (db.dump({"account": True, "orders": True, "list": True}),),
            )
            worker.import_order(con, 1, {
                "posting_number": "TWO-ITEMS",
                "status": "awaiting_packaging",
                "products": [
                    {"sku": "1", "offer_id": "KARTINA-1", "quantity": 1},
                    {"sku": "2", "offer_id": "KARTINA-2", "quantity": 1},
                ],
            })
        settings = {
            **self.settings,
            "start_enabled": True,
            "automation_enabled": True,
            "automation_enabled_at": "2026-09-23T00:00:00+00:00",
        }

        class Adapter:
            def start(self, _posting):
                raise AssertionError("Ambiguous posting must not be sent to Ozon")

        with self.assertRaises(OzonError) as raised:
            worker.sync_job(
                {"id": 123, "kind": "start_chat", "account_id": 1,
                 "payload": db.dump({"posting": "TWO-ITEMS"})},
                Adapter(), settings,
            )
        self.assertEqual(raised.exception.code, "start_item_ambiguous")
        page = self.client.get("/orders", headers=self.admin)
        self.assertIn("Несколько настроенных позиций", page.text)

    def test_inbound_event_dedup(self):
        _iid, _item, cid, _ = self.setup_instance()
        with db.transaction() as con:
            chat = con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
            value = {
                "message_id": "9000000000000000001",
                "user": {"type": "Customer"},
                "data": ["hello"],
            }
            worker.import_message(con, chat, value)
            worker.import_message(con, chat, value)
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1
            )
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM audit WHERE action='message.duplicate'"
                ).fetchone()[0],
                1,
            )

    def test_takeover_cancels_pending_bot_message(self):
        _iid, _item, cid, _ = self.setup_instance()
        with db.transaction() as con:
            scenarios.takeover(con, cid, "2")
        calls = []

        class Adapter:
            def __init__(self, *args):
                pass

            def send(self, *args):
                calls.append(args)
                return "sent"

            def close(self):
                pass

        worker.send_one(Adapter)
        self.assertEqual(calls, [])

    def test_concurrent_takeover_waits_for_send_then_blocks_followups(self):
        _iid, _item, cid, _ = self.setup_instance()
        sending = threading.Event()
        release = threading.Event()
        taken = threading.Event()
        calls = []

        class Adapter:
            def __init__(self, *args):
                pass

            def send(self, *args):
                calls.append(args)
                sending.set()
                release.wait(5)
                return "external-id"

            def close(self):
                pass

        def takeover():
            with db.transaction() as con:
                scenarios.takeover(con, cid, "2")
            taken.set()

        sender = threading.Thread(target=worker.send_one, args=(Adapter,))
        sender.start()
        self.assertTrue(sending.wait(5))
        manager = threading.Thread(target=takeover)
        manager.start()
        self.assertFalse(taken.wait(0.1))
        release.set()
        sender.join(5)
        manager.join(5)
        self.assertTrue(taken.is_set())
        self.assertEqual(len(calls), 1)
        with db.transaction() as con:
            self.assertEqual(
                con.execute("SELECT mode FROM chats").fetchone()[0], "manual"
            )

    def test_unknown_send_is_not_retried(self):
        self.setup_instance()
        calls = []

        class Adapter:
            def __init__(self, *args):
                pass

            def send(self, *args):
                calls.append(args)
                raise OzonError("timeout", unknown=True)

            def close(self):
                pass

        worker.send_one(Adapter)
        worker.send_one(Adapter)
        self.assertEqual(len(calls), 1)
        with db.transaction() as con:
            self.assertEqual(
                con.execute("SELECT state FROM outbox").fetchone()[0], "unknown"
            )

    def test_send_permission_failure_is_real_failure(self):
        self.setup_instance()

        class Adapter:
            def __init__(self, *args):
                pass

            def send(self, *args):
                raise OzonError("ozon_http_403")

            def close(self):
                pass

        worker.send_one(Adapter)
        with db.transaction() as con:
            self.assertEqual(
                con.execute("SELECT state FROM outbox").fetchone()[0], "failed"
            )
            self.assertEqual(
                con.execute("SELECT capabilities FROM accounts").fetchone()[0], "{}"
            )

    def test_only_definitive_send_failure_can_be_retried(self):
        self.setup_instance()
        with db.transaction() as con:
            outbox_id = con.execute("SELECT id FROM outbox").fetchone()[0]
            con.execute(
                "UPDATE outbox SET state='failed',error='ozon_http_403' WHERE id=?",
                (outbox_id,),
            )
        response = self.client.post(
            f"/outbox/{outbox_id}/resolve",
            headers=self.admin,
            data={"csrf": "csrf", "retry": "yes"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with db.transaction() as con:
            self.assertEqual(
                con.execute("SELECT state FROM outbox WHERE id=?", (outbox_id,)).fetchone()[0],
                "pending",
            )
            con.execute(
                "UPDATE outbox SET state='unknown',error='send_result_unknown' WHERE id=?",
                (outbox_id,),
            )
        response = self.client.post(
            f"/outbox/{outbox_id}/resolve",
            headers=self.admin,
            data={"csrf": "csrf", "retry": "yes"},
        )
        self.assertEqual(response.status_code, 422)
        with db.transaction() as con:
            self.assertEqual(
                con.execute("SELECT state FROM outbox WHERE id=?", (outbox_id,)).fetchone()[0],
                "unknown",
            )

    def test_ready_requires_confirmation_review_and_available_media(self):
        iid, item, cid, _ = self.setup_instance()
        with db.transaction() as con:
            with self.assertRaises(scenarios.Invalid):
                scenarios.approve(con, iid, "2")
            mid = self.image(con, cid, item)
            scenarios.advance(con, iid, {"media_ids": [mid]})
            scenarios.advance(con, iid, {"text": "Background"})
            scenarios.advance(con, iid, {"text": "yes"})
            (db.DATA / "media" / mid).unlink()
            with self.assertRaises(scenarios.Invalid):
                scenarios.approve(con, iid, "2")

    def test_cancelled_order_invalidates_ready(self):
        iid, item, _cid, _ = self.finish("portrait_background")
        with db.transaction() as con:
            scenarios.approve(con, iid, "2")
            scenarios.release_due(con)
            row = con.execute("SELECT * FROM items WHERE id=?", (item,)).fetchone()
            worker.import_order(
                con,
                1,
                {
                    "posting_number": row["posting"],
                    "status": "cancelled",
                    "products": [
                        {"sku": row["sku"], "offer_id": row["offer_id"], "quantity": 1}
                    ],
                },
            )
            self.assertEqual(
                con.execute("SELECT status FROM instances").fetchone()[0],
                "closed",
            )
            self.assertEqual(
                con.execute("SELECT mode FROM chats").fetchone()[0], "bot"
            )

    def test_routine_order_status_change_does_not_pause_bot(self):
        iid, item, chat_id, _ = self.setup_instance()
        with db.transaction() as con:
            row = con.execute("SELECT * FROM items WHERE id=?", (item,)).fetchone()
            worker.import_order(
                con,
                1,
                {
                    "posting_number": row["posting"],
                    "status": "awaiting_deliver",
                    "products": [{"sku": row["sku"], "offer_id": row["offer_id"], "quantity": 1}],
                },
            )
            self.assertEqual(con.execute("SELECT status FROM instances WHERE id=?", (iid,)).fetchone()[0], "collecting")
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "bot")

    def test_cancelled_order_stops_legacy_system_takeover_without_manager(self):
        iid, item, chat_id, _ = self.setup_instance()
        with db.transaction() as con:
            con.execute("UPDATE instances SET status='needs_manager' WHERE id=?", (iid,))
            scenarios.takeover(con, chat_id, "system")
            event_id = con.execute(
                "INSERT INTO events(account_id,external_key,chat_id,body) VALUES(1,?,?,?)",
                ("cancelled-buyer-reply", chat_id, db.dump({"item_id": item, "epoch": 1, "text": "Да"})),
            ).lastrowid
            row = con.execute("SELECT * FROM items WHERE id=?", (item,)).fetchone()
            worker.import_order(
                con,
                1,
                {
                    "posting_number": row["posting"],
                    "status": "cancelled",
                    "products": [{"sku": row["sku"], "offer_id": row["offer_id"], "quantity": 1}],
                },
            )
            self.assertEqual(con.execute("SELECT status FROM instances WHERE id=?", (iid,)).fetchone()[0], "closed")
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "bot")
            self.assertEqual(con.execute("SELECT state FROM outbox WHERE instance_id=?", (iid,)).fetchone()[0], "cancelled")
            self.assertEqual(con.execute("SELECT state FROM events WHERE id=?", (event_id,)).fetchone()[0], "processed")
            with self.assertRaisesRegex(scenarios.Invalid, "Заказ отменён"):
                scenarios.resume(con, chat_id, "start", "1")
        page = self.client.get(f"/chats/{chat_id}", headers=self.admin)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Заказ отменён", page.text)
        self.assertNotIn("Бот работает", page.text)

    def test_cancelled_order_does_not_override_human_takeover(self):
        iid, item, chat_id, _ = self.setup_instance()
        with db.transaction() as con:
            scenarios.takeover(con, chat_id, "2")
            row = con.execute("SELECT * FROM items WHERE id=?", (item,)).fetchone()
            worker.import_order(
                con,
                1,
                {
                    "posting_number": row["posting"],
                    "status": "cancelled",
                    "products": [{"sku": row["sku"], "offer_id": row["offer_id"], "quantity": 1}],
                },
            )
            self.assertEqual(con.execute("SELECT status FROM instances WHERE id=?", (iid,)).fetchone()[0], "closed")
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "manual")

    def test_cancelled_order_ignores_later_buyer_reply_even_with_unknown_send(self):
        _iid, item, chat_id, _ = self.setup_instance()
        with db.transaction() as con:
            con.execute("UPDATE outbox SET state='unknown' WHERE chat_id=?", (chat_id,))
            row = con.execute("SELECT * FROM items WHERE id=?", (item,)).fetchone()
            worker.import_order(
                con,
                1,
                {
                    "posting_number": row["posting"],
                    "status": "cancelled",
                    "products": [{"sku": row["sku"], "offer_id": row["offer_id"], "quantity": 1}],
                },
            )
            event_id = con.execute(
                "INSERT INTO events(account_id,external_key,chat_id,body) VALUES(1,?,?,?)",
                ("late-cancelled-reply", chat_id, db.dump({"item_id": item, "epoch": 0, "text": "Да", "attachment_error": True})),
            ).lastrowid
        worker.tick()
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT state FROM events WHERE id=?", (event_id,)).fetchone()[0], "processed")
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "bot")
            self.assertEqual(con.execute("SELECT state FROM outbox WHERE chat_id=?", (chat_id,)).fetchone()[0], "unknown")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM outbox WHERE chat_id=?", (chat_id,)).fetchone()[0], 1)

    def test_cancelled_order_closes_uncertain_chat_starts_without_retry(self):
        _iid, item, _chat_id, _ = self.setup_instance()
        with db.transaction() as con:
            product = con.execute("SELECT * FROM items WHERE id=?", (item,)).fetchone()
            first_id = con.execute(
                "INSERT INTO jobs(kind,account_id,payload,state,error,created_at,finished_at) "
                "VALUES('start_chat',1,?,'unknown','transport_unknown',?,?)",
                (db.dump({"posting": "CANCELLED-1"}), db.now(), db.now()),
            ).lastrowid
            worker.import_order(
                con,
                1,
                {
                    "posting_number": "CANCELLED-1",
                    "status": "cancelled",
                    "products": [{"sku": product["sku"], "offer_id": product["offer_id"], "quantity": 1}],
                },
            )
            self.assertEqual(con.execute("SELECT state FROM jobs WHERE id=?", (first_id,)).fetchone()[0], "cancelled")
            stale_id = con.execute(
                "INSERT INTO jobs(kind,account_id,payload,state,error,created_at,finished_at) "
                "VALUES('start_chat',1,?,'unknown','transport_unknown',?,?)",
                (db.dump({"posting": "CANCELLED-1"}), db.now(), db.now()),
            ).lastrowid
        orders = self.client.get("/orders", headers=self.admin)
        self.assertEqual(orders.status_code, 200)
        self.assertIn("Заказ отменён. Бот не начинает", orders.text)
        self.assertNotIn("Требует сверки", orders.text)
        settings = self.client.get("/settings/ozon", headers=self.admin)
        self.assertNotIn("Создание чата не подтверждено — отправление CANCELLED-1", settings.text)
        with patch.object(worker, "send_one", return_value=False):
            worker.tick()
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT state FROM jobs WHERE id=?", (stale_id,)).fetchone()[0], "cancelled")
        audit = self.client.get("/audit", headers=self.admin)
        self.assertIn("Попытка запуска чата закрыта без повторного запроса к Ozon", audit.text)

    def test_rbac_pages_forms_exports_and_object_ids(self):
        iid, _item, cid, _ = self.setup_instance()
        for path in (
            "/settings",
            "/scenarios",
            "/scenarios/1",
            "/catalog",
            "/orders",
            "/audit",
            f"/briefs/{iid}/export",
            f"/chats/{cid}",
        ):
            self.assertEqual(
                self.client.get(path, headers=self.manager).status_code, 403, path
            )
        for path in (
            "/settings/ozon",
            "/settings/automation",
            "/accounts",
            "/users",
            "/mappings",
            "/templates",
            "/scenarios",
            "/scenarios/1/publish",
            "/accounts/1/sync",
            "/outbox/1/resolve",
        ):
            self.assertEqual(
                self.client.post(
                    path, headers=self.manager, data={"csrf": "csrf"}
                ).status_code,
                403,
                path,
            )
        self.assertEqual(self.client.get("/").status_code, 401)
        self.assertEqual(
            self.client.post("/settings/automation", headers=self.admin).status_code, 403
        )

    def test_private_attachments_and_corrupt_files(self):
        _, item, cid, _ = self.setup_instance()
        with db.transaction() as con:
            mid = self.image(con, cid, item)
            with self.assertRaises(scenarios.Invalid):
                media.store(con, b"not an image", self.settings, cid, item)
        self.assertEqual(
            self.client.get(f"/media/{mid}", headers=self.manager).status_code, 403
        )
        self.assertEqual(
            self.client.get(f"/media/{mid}", headers=self.admin).status_code, 200
        )
        with self.assertRaises(scenarios.Invalid):
            media.download("http://127.0.0.1/secret", self.settings)
        with self.assertRaises(scenarios.Invalid):
            media.download("https://evil.example/secret", self.settings)

    def test_ozon_markdown_image_is_stored_and_accepted_as_photo(self):
        instance_id, item_id, chat_id, _ = self.setup_instance(
            posting="PHOTO-ORDER", external_chat="PHOTO-CHAT"
        )
        url = "https://api-seller.ozon.ru/v2/chat/file/messenger-rotated-3/image.png"
        raw = {
            "message_id": "ozon-photo-message", "created_at": db.now(),
            "user": {"type": "Customer"}, "data": [f"![]({url})"],
        }
        self.assertEqual(worker._message_contents(raw), ("", [url]))
        image = io.BytesIO()
        Image.new("RGB", (4, 4)).save(image, format="PNG")
        requests = []

        def file_response(request):
            requests.append((request.url.path, request.headers.get("Api-Key")))
            return httpx.Response(200, headers={"Content-Type": "image/png"}, content=image.getvalue())

        class Adapter:
            def __init__(self):
                self.client = httpx.Client(
                    headers={"Client-Id": "test", "Api-Key": "test-secret"},
                    transport=httpx.MockTransport(file_response),
                )

            def seller_info(self):
                return {}

            def order_pages(self, *_args):
                return iter([])

            def chats(self):
                return iter([{"chat": {"chat_id": "PHOTO-CHAT", "chat_type": "BUYER_SELLER"}}])

            def history(self, _chat_id):
                return iter([raw])

        with db.transaction() as con:
            con.execute("UPDATE outbox SET state='sent',external_id='prompt-id' WHERE chat_id=?", (chat_id,))
        adapter = Adapter()
        try:
            with patch("app.media.socket.getaddrinfo", return_value=[
                (2, 1, 6, "", ("8.8.8.8", 443)),
            ]):
                worker.sync_job(
                    {"kind": "sync", "account_id": 1, "payload": "{}"}, adapter,
                    {**self.settings, "sync_since": "2026-09-23T00:00:00+00:00"},
                )
        finally:
            adapter.client.close()
        self.assertEqual(requests, [("/v2/chat/file/messenger-rotated-3/image.png", "test-secret")])
        with db.transaction() as con:
            message = con.execute(
                "SELECT body,media_ids FROM messages WHERE external_id='ozon-photo-message'"
            ).fetchone()
            media_ids = json.loads(message["media_ids"])
            self.assertEqual(message["body"], "")
            self.assertEqual(len(media_ids), 1)
            self.assertTrue((db.DATA / "media" / media_ids[0]).is_file())
            event = con.execute("SELECT * FROM events WHERE chat_id=?", (chat_id,)).fetchone()
            self.assertEqual(json.loads(event["body"])["media_ids"], media_ids)
            self.assertFalse(json.loads(event["body"])["attachment_error"])
            worker.process_event(con, event)
            instance = con.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
            self.assertEqual(json.loads(instance["fields"])["photos"], media_ids)
            self.assertEqual(instance["status"], "collecting")
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "bot")
            self.assertEqual(con.execute("SELECT item_id FROM media WHERE id=?", (media_ids[0],)).fetchone()[0], item_id)

    def test_cancelled_bot_reply_does_not_appear_as_sent_message(self):
        _, _, chat_id, _ = self.setup_instance()
        with db.transaction() as con:
            con.execute("UPDATE outbox SET state='cancelled',body='NOT SENT TO BUYER' WHERE chat_id=?", (chat_id,))
        page = self.client.get(f"/chats/{chat_id}", headers=self.admin)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("NOT SENT TO BUYER", page.text)

    def test_manager_assignment_and_manual_message_idempotency(self):
        _iid, _item, cid, _ = self.setup_instance()
        with db.transaction() as con:
            con.execute("INSERT INTO assignments VALUES(?,'2')", (cid,))
        self.assertEqual(
            self.client.get(f"/chats/{cid}", headers=self.manager).status_code, 200
        )
        for _ in range(2):
            response = self.client.post(
                f"/chats/{cid}/send",
                headers=self.manager,
                data={
                    "csrf": "csrf",
                    "text": "Approved manual answer",
                    "dedup": "same",
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)
        with db.transaction() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM outbox WHERE actor='manager:2'"
                ).fetchone()[0],
                1,
            )

    def test_pages_render_and_secrets_are_not_exposed(self):
        self.setup_instance()
        for path in (
            "/",
            "/chats/1",
            "/settings",
            "/scenarios",
            "/scenarios/1",
            "/catalog",
            "/orders",
            "/audit",
        ):
            response = self.client.get(path, headers=self.admin)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn("PRIVATE_TEST_KEY", response.text)
        with db.transaction() as con:
            self.assertNotIn(
                "PRIVATE_TEST_KEY",
                str([tuple(r) for r in con.execute("SELECT * FROM audit")]),
            )

    def test_adapter_timeout_403_and_invalid_json(self):
        with db.transaction() as con:
            account = dict(con.execute("SELECT * FROM accounts").fetchone())
        for status, body, unknown in [
            (403, {}, False),
            (500, {}, True),
            (200, {"unexpected": "data"}, True),
        ]:
            adapter = OzonAdapter(
                account,
                self.settings,
                httpx.MockTransport(
                    lambda request, code=status, data=body: httpx.Response(
                        code, json=data
                    )
                ),
            )
            with self.assertRaises(OzonError) as exc:
                adapter.send("chat", "approved text")
            self.assertEqual(exc.exception.unknown, unknown)
            self.assertNotIn("PRIVATE_TEST_KEY", str(exc.exception))
            adapter.close()

    def test_adapter_pagination_detects_stalled_cursor(self):
        with db.transaction() as con:
            account = dict(con.execute("SELECT * FROM accounts").fetchone())
        adapter = OzonAdapter(
            account,
            self.settings,
            httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"chats": [], "has_next": True, "cursor": "same"}
                )
            ),
        )
        with self.assertRaises(OzonError):
            list(adapter.chats())
        adapter.close()

    def test_adapter_reads_paginated_seller_articles(self):
        with db.transaction() as con:
            account = dict(con.execute("SELECT * FROM accounts").fetchone())
        cursors = []

        def handler(request):
            self.assertEqual(request.url.path, "/v3/product/list")
            body = json.loads(request.content)
            self.assertEqual(body["filter"], {"visibility": "ALL"})
            cursors.append(body["last_id"])
            if body["last_id"]:
                return httpx.Response(200, json={"result": {
                    "items": [{"offer_id": "SELLER-B"}], "last_id": "",
                }})
            return httpx.Response(200, json={"result": {
                "items": [{"offer_id": "SELLER-A"}], "last_id": "next",
            }})

        adapter = OzonAdapter(account, {}, httpx.MockTransport(handler))
        try:
            self.assertEqual(
                [row["offer_id"] for row in adapter.products()],
                ["SELLER-A", "SELLER-B"],
            )
            self.assertEqual(cursors, ["", "next"])
        finally:
            adapter.close()

    def test_send_accepts_string_message_id_from_ozon(self):
        with db.transaction() as con:
            account = dict(con.execute("SELECT * FROM accounts").fetchone())
        for payload in ({"result": "message-123"}, {"result": {"message_id": "message-123"}}):
            adapter = OzonAdapter(
                account,
                self.settings,
                httpx.MockTransport(lambda _request, value=payload: httpx.Response(200, json=value)),
            )
            try:
                self.assertEqual(adapter.send("chat", "approved text"), "message-123")
            finally:
                adapter.close()

    def test_send_file_uses_ozon_binary_contract_and_unknown_ack(self):
        with db.transaction() as con:
            account = dict(con.execute("SELECT * FROM accounts").fetchone())
        data = b"validated-image-bytes"
        def handler(request):
            self.assertEqual(request.url.path, "/v1/chat/send/file")
            body = json.loads(request.content)
            self.assertEqual(body["chat_id"], "chat")
            self.assertEqual(body["name"], "folio-image.png")
            self.assertEqual(base64.b64decode(body["base64_content"]), data)
            return httpx.Response(200, json={"result": "success"})
        adapter = OzonAdapter(account, self.settings, httpx.MockTransport(handler))
        try:
            with self.assertRaises(OzonError) as caught:
                adapter.send_file("chat", "folio-image.png", data)
            self.assertTrue(caught.exception.unknown)
        finally:
            adapter.close()

    def test_unknown_file_send_reconciles_only_matching_image(self):
        _instance, item_id, chat_id, _ = self.setup_instance()
        with db.transaction() as con:
            mid = self.image(con, chat_id, item_id)
            original = (db.DATA / "media" / mid).read_bytes()
            con.execute("DELETE FROM outbox WHERE chat_id=?", (chat_id,))
            outbox_id = con.execute(
                "INSERT INTO outbox(chat_id,actor,body,dedup,epoch,state,created_at,kind,media_id) "
                "VALUES(?,'manager:1','Изображение макета','test-file',0,'unknown',?,'file',?)",
                (chat_id, db.now(), mid),
            ).lastrowid
            chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            raw = {"message_id": "ozon-file-1", "created_at": db.now(),
                   "user": {"type": "seller"}, "data": ["![](https://api-seller.ozon.ru/v2/chat/file/1)"]}
            worker.reconcile_outbox(con, chat, [raw], {"ozon-file-1": hashlib.sha256(original).hexdigest()})
            self.assertEqual(con.execute("SELECT state FROM outbox WHERE id=?", (outbox_id,)).fetchone()[0], "sent")

    def test_reconciled_manager_mockup_recovers_bot_before_approval_prompt(self):
        instance_id, item_id, chat_id, _ = self.setup_instance()
        graph_value = {"nodes": [
            {"id": "start", "kind": "start", "next": "mockup"},
            {"id": "mockup", "kind": "await_mockup", "next": "approval"},
            {"id": "approval", "kind": "approval", "text": "Подтвердите макет",
             "accept": "ДА", "reject": "НЕТ", "hours": 6,
             "next": "end", "error": "end"},
            {"id": "end", "kind": "end"},
        ]}
        with db.transaction() as con:
            version_id = con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0]
            con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump(graph_value), version_id))
            con.execute("UPDATE instances SET node='mockup',status='waiting_mockup',prompted=0 WHERE id=?", (instance_id,))
            con.execute("DELETE FROM outbox WHERE chat_id=?", (chat_id,))
            mid = self.image(con, chat_id, item_id)
            digest = hashlib.sha256((db.DATA / "media" / mid).read_bytes()).hexdigest()
            outbox_id = con.execute(
                "INSERT INTO outbox(chat_id,instance_id,actor,body,dedup,epoch,state,created_at,kind,media_id) "
                "VALUES(?,?,'manager:2','Изображение макета','mockup-unknown',0,'unknown',?,'file',?)",
                (chat_id, instance_id, db.now(), mid),
            ).lastrowid
            seller_message = {
                "message_id": "ozon-mockup", "created_at": db.now(),
                "user": {"type": "seller"},
                "data": ["![](https://api-seller.ozon.ru/v2/chat/file/mockup)"],
            }
            chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            worker.import_message(con, chat, seller_message)
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "manual")
            worker.reconcile_outbox(con, chat, [seller_message], {"ozon-mockup": digest})
            self.assertEqual(con.execute("SELECT state FROM outbox WHERE id=?", (outbox_id,)).fetchone()[0], "sent")
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "bot")
            self.assertEqual(
                tuple(con.execute("SELECT node,status FROM instances WHERE id=?", (instance_id,)).fetchone()),
                ("approval", "waiting_approval"),
            )
            prompt = con.execute(
                "SELECT body,state FROM outbox WHERE instance_id=? AND kind='text'", (instance_id,),
            ).fetchone()
            self.assertEqual(tuple(prompt), ("Подтвердите макет", "pending"))
            scenarios.takeover(con, chat_id, "2")
            worker._undo_false_takeover(con, chat, {"ozon-mockup"})
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "manual")

    def test_reconciled_bot_mockup_recovers_bot_and_enters_approval(self):
        instance_id, item_id, chat_id, _ = self.setup_instance()
        graph_value = {"nodes": [
            {"id": "start", "kind": "start", "next": "mockup"},
            {"id": "mockup", "kind": "send_mockup", "next": "approval"},
            {"id": "approval", "kind": "approval", "text": "Подтвердите макет",
             "accept": "ДА", "reject": "НЕТ", "hours": 6,
             "next": "end", "error": "end"},
            {"id": "end", "kind": "end"},
        ]}
        with db.transaction() as con:
            version_id = con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0]
            con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump(graph_value), version_id))
            con.execute("UPDATE instances SET node='mockup',status='waiting_integration',prompted=0 WHERE id=?",
                        (instance_id,))
            con.execute("DELETE FROM outbox WHERE chat_id=?", (chat_id,))
            mid = self.image(con, chat_id, item_id)
            digest = hashlib.sha256((db.DATA / "media" / mid).read_bytes()).hexdigest()
            outbox_id = con.execute(
                "INSERT INTO outbox(chat_id,instance_id,actor,body,dedup,epoch,state,created_at,kind,media_id) "
                "VALUES(?,?,'bot','Макет для согласования',?,0,'unknown',?,'file',?)",
                (chat_id, instance_id, f"mockup:{instance_id}:mockup", db.now(), mid),
            ).lastrowid
            seller_message = {
                "message_id": "ozon-bot-mockup", "created_at": db.now(),
                "user": {"type": "seller"},
                "data": ["![](https://api-seller.ozon.ru/v2/chat/file/mockup)"],
            }
            chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            worker.import_message(con, chat, seller_message)
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "manual")
            worker.reconcile_outbox(con, chat, [seller_message], {"ozon-bot-mockup": digest})
            self.assertEqual(con.execute("SELECT state FROM outbox WHERE id=?", (outbox_id,)).fetchone()[0], "sent")
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "bot")
            self.assertEqual(
                tuple(con.execute("SELECT node,status FROM instances WHERE id=?", (instance_id,)).fetchone()),
                ("approval", "waiting_approval"),
            )
            prompt = con.execute(
                "SELECT body,state FROM outbox WHERE instance_id=? AND kind='text'", (instance_id,),
            ).fetchone()
            self.assertEqual(tuple(prompt), ("Подтвердите макет", "pending"))

    def test_previously_reconciled_bot_mockup_resumes_on_sync(self):
        instance_id, item_id, chat_id, _ = self.setup_instance()
        graph_value = {"nodes": [
            {"id": "start", "kind": "start", "next": "mockup"},
            {"id": "mockup", "kind": "send_mockup", "next": "approval"},
            {"id": "approval", "kind": "approval", "text": "Подтвердите макет",
             "accept": "ДА", "reject": "НЕТ", "hours": 6,
             "next": "end", "error": "end"},
            {"id": "end", "kind": "end"},
        ]}
        with db.transaction() as con:
            version_id = con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0]
            con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump(graph_value), version_id))
            con.execute("UPDATE instances SET node='mockup',status='waiting_integration' WHERE id=?", (instance_id,))
            con.execute("DELETE FROM outbox WHERE chat_id=?", (chat_id,))
            mid = self.image(con, chat_id, item_id)
            con.execute(
                "INSERT INTO outbox(chat_id,instance_id,actor,body,dedup,epoch,state,created_at,kind,media_id) "
                "VALUES(?,?,'bot','Макет для согласования',?,0,'unknown',?,'file',?)",
                (chat_id, instance_id, f"mockup:{instance_id}:mockup", db.now(), mid),
            )
            chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            worker.import_message(con, chat, {
                "message_id": "ozon-old-mockup", "created_at": db.now(),
                "user": {"type": "seller"},
                "data": ["![](https://api-seller.ozon.ru/v2/chat/file/old)"],
            })
            con.execute(
                "UPDATE outbox SET state='sent',external_id='ozon-old-mockup' "
                "WHERE dedup=?", (f"mockup:{instance_id}:mockup",),
            )
            worker.recover_sent_mockup(con, chat)
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "bot")
            self.assertEqual(
                tuple(con.execute("SELECT node,status FROM instances WHERE id=?", (instance_id,)).fetchone()),
                ("approval", "waiting_approval"),
            )
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM outbox WHERE instance_id=? AND kind='text' AND state='pending'",
                (instance_id,),
            ).fetchone()[0], 1)

    def test_send_acknowledgement_is_not_a_message_id(self):
        self.setup_instance()

        def adapter_factory(account, settings):
            return OzonAdapter(
                account, settings,
                httpx.MockTransport(lambda _request: httpx.Response(200, json={"result": "success"})),
            )

        self.assertTrue(worker.send_one(adapter_factory))
        with db.transaction() as con:
            outbox = con.execute("SELECT * FROM outbox").fetchone()
            self.assertEqual((outbox["state"], outbox["external_id"]), ("unknown", None))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            chat = con.execute("SELECT * FROM chats WHERE id=?", (outbox["chat_id"],)).fetchone()
            seller = {
                "message_id": "actual-ozon-message-id", "user": {"type": "Seller"},
                "data": [outbox["body"]], "created_at": db.now(),
            }
            worker.reconcile_outbox(con, chat, [seller])
            self.assertEqual(tuple(con.execute(
                "SELECT state,external_id FROM outbox WHERE id=?", (outbox["id"],)
            ).fetchone()), ("sent", "actual-ozon-message-id"))
            worker.import_message(con, chat, seller)
            self.assertEqual(con.execute(
                "SELECT mode FROM chats WHERE id=?", (chat["id"],)
            ).fetchone()[0], "bot")

    def test_unknown_send_recovers_from_history_before_buyer_reply(self):
        _instance, item_id, chat_id, _scenario = self.setup_instance()
        with db.transaction() as con:
            con.execute("UPDATE outbox SET body='Line 1\nLine 2' WHERE chat_id=?", (chat_id,))
            config = db.config(con)
            config["sync_since"] = "2026-09-23T00:00:00+00:00"
            con.execute("UPDATE settings SET value=?", (db.dump(config),))

        class UncertainAdapter:
            def __init__(self, *_args):
                pass

            def send(self, *_args):
                raise OzonError("read_timeout_unknown", unknown=True)

            def close(self):
                pass

        self.assertTrue(worker.send_one(UncertainAdapter))
        with db.transaction() as con:
            chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            worker.import_message(con, chat, {
                "message_id": "buyer-456", "user": {"type": "Customer"},
                "data": ["photos_done"], "created_at": db.now(),
            })
            photo_id = self.image(con, chat_id, item_id)
            event = con.execute("SELECT id,body FROM events").fetchone()
            event_body = json.loads(event["body"])
            event_body["media_ids"] = [photo_id]
            con.execute(
                "UPDATE events SET body=? WHERE id=?",
                (db.dump(event_body), event["id"]),
            )
            self.assertEqual(con.execute("SELECT state FROM outbox").fetchone()[0], "unknown")
            self.assertEqual(con.execute("SELECT state FROM events").fetchone()[0], "pending")

        class HistoryAdapter:
            def __init__(self, *_args):
                pass

            def seller_info(self):
                return {"seller": True}

            def order_pages(self, *_args):
                yield [], None

            def chats(self):
                yield {"chat": {"chat_id": "CHAT", "chat_type": "BUYER_SELLER"}}

            def history(self, _chat_id):
                return iter([
                    {"message_id": "buyer-456", "user": {"type": "Customer"},
                     "data": ["photos_done"], "created_at": db.now()},
                    {"message_id": "seller-123", "user": {"type": "Seller"},
                     "data": ["Line 1  \nLine 2"], "created_at": db.now()},
                ])

            def close(self):
                pass

        with patch("app.worker.OzonAdapter", HistoryAdapter):
            self.assertTrue(worker.tick())  # sync, while the buyer event waits
            with db.transaction() as con:
                self.assertEqual(
                    tuple(con.execute("SELECT state,external_id FROM outbox").fetchone()),
                    ("sent", "seller-123"),
                )
                self.assertEqual(
                    con.execute("SELECT mode FROM chats").fetchone()[0], "bot",
                    [tuple(row) for row in con.execute("SELECT action,object_type,object_id FROM audit ORDER BY id")],
                )
            self.assertTrue(worker.tick())  # now apply the buyer event
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT state FROM events").fetchone()[0], "processed")
            self.assertEqual(con.execute("SELECT mode FROM chats").fetchone()[0], "bot")

    def test_reused_chat_reconciles_unique_send_with_slight_ozon_clock_skew(self):
        _, first_item, first_chat, _ = self.setup_instance(
            posting="OLD-POSTING", external_chat="SHARED-CHAT"
        )
        _, _, second_chat, _ = self.setup_instance(
            posting="NEW-POSTING", external_chat="SHARED-CHAT"
        )
        with db.transaction() as con:
            first = con.execute("SELECT sku,offer_id FROM items WHERE id=?", (first_item,)).fetchone()
            worker.import_order(con, 1, {
                "posting_number": "OLD-POSTING", "status": "cancelled",
                "products": [{"sku": first["sku"], "offer_id": first["offer_id"],
                              "name": "Painting", "quantity": 1}],
            })
            first_out = con.execute("SELECT id,body FROM outbox WHERE chat_id=?", (first_chat,)).fetchone()
            second_out = con.execute("SELECT id,body FROM outbox WHERE chat_id=?", (second_chat,)).fetchone()
            self.assertEqual(first_out["body"], second_out["body"])
            con.execute("UPDATE outbox SET state='sent',external_id='previous-message' WHERE id=?", (first_out["id"],))
            queued_at = db.now()
            con.execute(
                "UPDATE outbox SET state='unknown',error='send_result_unknown',created_at=? WHERE id=?",
                (queued_at, second_out["id"]),
            )
        sent_at = datetime.fromisoformat(queued_at) - timedelta(milliseconds=438)
        earlier = datetime.fromisoformat(queued_at) - timedelta(hours=1)

        class Adapter:
            def seller_info(self):
                return {}

            def order_pages(self, *_args):
                return iter([])

            def chats(self):
                return iter([{"chat": {"chat_id": "SHARED-CHAT", "chat_type": "BUYER_SELLER"}}])

            def history(self, _chat_id):
                return iter([
                    {"message_id": "buyer-reply", "user": {"type": "Customer"},
                     "data": ["Ответ"], "created_at": db.now()},
                    {"message_id": "delivered-message", "user": {"type": "Seller"},
                     "data": [second_out["body"]], "created_at": sent_at.isoformat()},
                    {"message_id": "previous-message", "user": {"type": "Seller"},
                     "data": [first_out["body"]], "created_at": earlier.isoformat()},
                ])

        worker.sync_job(
            {"kind": "sync", "account_id": 1, "payload": "{}"}, Adapter(),
            {**self.settings, "sync_since": "2026-09-23T00:00:00+00:00"},
        )
        with db.transaction() as con:
            self.assertEqual(tuple(con.execute(
                "SELECT state,external_id FROM outbox WHERE id=?", (second_out["id"],)
            ).fetchone()), ("sent", "delivered-message"))
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (second_chat,)).fetchone()[0], "bot")
            self.assertEqual([row[0] for row in con.execute(
                "SELECT chat_id FROM messages WHERE external_id='delivered-message'"
            )], [second_chat])
            self.assertEqual([row[0] for row in con.execute(
                "SELECT chat_id FROM events WHERE external_key LIKE 'message:%buyer-reply'"
            )], [second_chat])

    def test_old_identical_seller_message_does_not_confirm_unknown_send(self):
        _, _, chat_id, _ = self.setup_instance()
        with db.transaction() as con:
            outbox = con.execute("SELECT * FROM outbox WHERE chat_id=?", (chat_id,)).fetchone()
            con.execute("UPDATE outbox SET state='unknown' WHERE id=?", (outbox["id"],))
            chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            worker.reconcile_outbox(con, chat, [{
                "message_id": "old-identical-message", "user": {"type": "Seller"},
                "data": [outbox["body"]],
                "created_at": (datetime.fromisoformat(outbox["created_at"]) - timedelta(seconds=10)).isoformat(),
            }])
            self.assertEqual(tuple(con.execute(
                "SELECT state,external_id FROM outbox WHERE id=?", (outbox["id"],)
            ).fetchone()), ("unknown", None))

    def test_two_identical_unknown_sends_in_one_ozon_chat_are_not_guessed(self):
        self.setup_instance(posting="POSTING-A", external_chat="SHARED-CHAT")
        self.setup_instance(posting="POSTING-B", external_chat="SHARED-CHAT")
        with db.transaction() as con:
            queued_at = db.now()
            con.execute(
                "UPDATE outbox SET state='unknown',error='send_result_unknown',created_at=?",
                (queued_at,),
            )
            body = con.execute("SELECT body FROM outbox LIMIT 1").fetchone()[0]
        sent_at = datetime.fromisoformat(queued_at) - timedelta(milliseconds=438)

        class Adapter:
            def seller_info(self):
                return {}

            def order_pages(self, *_args):
                return iter([])

            def chats(self):
                return iter([{"chat": {"chat_id": "SHARED-CHAT", "chat_type": "BUYER_SELLER"}}])

            def history(self, _chat_id):
                return iter([{"message_id": "ambiguous-seller-message",
                              "user": {"type": "Seller"}, "data": [body],
                              "created_at": sent_at.isoformat()}])

        worker.sync_job(
            {"kind": "sync", "account_id": 1, "payload": "{}"}, Adapter(),
            {**self.settings, "sync_since": "2026-09-23T00:00:00+00:00"},
        )
        with db.transaction() as con:
            self.assertEqual([row[0] for row in con.execute(
                "SELECT state FROM outbox ORDER BY id"
            )], ["unknown", "unknown"])
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM messages WHERE external_id='ambiguous-seller-message'"
            ).fetchone()[0], 0)

    def test_history_repair_restores_bot_paused_by_legacy_unknown_send(self):
        _instance, _item, chat_id, _scenario = self.setup_instance()
        with db.transaction() as con:
            outbox = con.execute("SELECT id,body FROM outbox WHERE chat_id=?", (chat_id,)).fetchone()
            con.execute(
                "UPDATE outbox SET state='unknown',error='send_internal_unknown' WHERE id=?",
                (outbox["id"],),
            )
            seller = {
                "message_id": "seller-123", "user": {"type": "Seller"},
                "data": [outbox["body"]], "created_at": db.now(),
            }
            buyer = {
                "message_id": "buyer-456", "user": {"type": "Customer"},
                "data": ["reply"], "created_at": db.now(),
            }
            chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            worker.import_message(con, chat, seller)
            chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            worker.import_message(con, chat, buyer)
            event = con.execute("SELECT * FROM events WHERE chat_id=?", (chat_id,)).fetchone()
            worker.process_event(con, event)
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "manual")
            self.assertEqual(con.execute("SELECT state FROM events WHERE id=?", (event["id"],)).fetchone()[0], "processed")
            chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            worker.reconcile_outbox(con, chat, [buyer, seller])
            self.assertEqual(
                tuple(con.execute("SELECT state,external_id FROM outbox WHERE id=?", (outbox["id"],)).fetchone()),
                ("sent", "seller-123"),
            )
            self.assertEqual(
                con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "bot",
                [tuple(row) for row in con.execute("SELECT id,actor,action,object_id FROM audit WHERE chat_id=? ORDER BY id", (chat_id,))],
            )
            self.assertEqual(con.execute("SELECT state FROM events WHERE id=?", (event["id"],)).fetchone()[0], "pending")

    def test_unknown_send_has_no_manual_reconciliation_form(self):
        self.setup_instance()
        with db.transaction() as con:
            con.execute("UPDATE outbox SET state='unknown',error='send_internal_unknown'")
        page = self.client.get("/audit", headers=self.admin)
        self.assertEqual(page.status_code, 200)
        html = page.content.decode("utf-8")
        self.assertIn('class="folio-diagnostic-item"', html)
        self.assertNotIn('name="external_id"', html)

    def test_chat_start_has_longer_timeout_and_preserves_unknown_result(self):
        with db.transaction() as con:
            account = dict(con.execute("SELECT * FROM accounts").fetchone())
        observed = []

        def answer(request):
            observed.append((request.url.path, request.extensions["timeout"]["read"]))
            if request.url.path == "/v1/chat/start":
                raise httpx.ReadTimeout("test timeout", request=request)
            return httpx.Response(200, json={})

        with patch.dict(os.environ, {"FOLIO_OZON_HTTP_TIMEOUT_SECONDS": ""}):
            adapter = OzonAdapter(account, self.settings, httpx.MockTransport(answer))
            adapter.post("/read", {})
            with self.assertRaises(OzonError) as raised:
                adapter.start("POSTING-1")
            adapter.close()
        self.assertEqual(observed, [("/read", 5.0), ("/v1/chat/start", 30.0)])
        self.assertEqual(raised.exception.code, "read_timeout_unknown")
        self.assertTrue(raised.exception.unknown)

    def test_unknown_chat_start_is_not_presented_as_sent_message(self):
        with db.transaction() as con:
            con.execute("UPDATE accounts SET error='transport_unknown' WHERE id=1")
            con.execute(
                "INSERT INTO jobs(kind,account_id,payload,state,error,created_at,finished_at) "
                "VALUES('start_chat',1,?,'unknown','transport_unknown',?,?)",
                (db.dump({"posting": "POSTING-1"}), db.now(), db.now()),
            )
        for path in ("/settings/ozon", "/audit"):
            response = self.client.get(path, headers=self.admin)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Создание чата не подтверждено", response.text)
            self.assertNotIn("Результат отправки неизвестен", response.text)
        with db.transaction() as con:
            con.execute("UPDATE accounts SET error=NULL WHERE id=1")
        response = self.client.get("/settings/ozon", headers=self.admin)
        self.assertIn("Создание чата не подтверждено", response.text)

    def test_unknown_chat_start_does_not_disable_other_orders(self):
        settings = {
            **self.settings,
            "start_enabled": True,
            "automation_enabled": True,
            "automation_enabled_at": "2026-09-23T00:00:00+00:00",
        }
        with db.transaction() as con:
            con.execute("UPDATE settings SET value=?", (db.dump(settings),))
            con.execute(
                "UPDATE accounts SET capabilities=?,error=NULL WHERE id=1",
                (db.dump({"account": True, "orders": True, "list": True}),),
            )
            sid = con.execute(
                "INSERT INTO scenarios(name,product_type,draft) "
                "VALUES('Portrait','portrait_background',?)",
                (db.dump(graph("portrait_background")),),
            ).lastrowid
            scenarios.publish(con, sid, "1")
            con.execute(
                "INSERT INTO mappings(account_id,sku,product_type,scenario_id,key_kind) "
                "VALUES(1,'KARTINA-1','portrait_background',?,'seller_article')",
                (sid,),
            )
            worker.import_order(con, 1, {
                "posting_number": "POSTING-1",
                "in_process_at": "2026-09-24T08:00:00Z",
                "status": "awaiting_packaging",
                "products": [{"sku": "1001", "offer_id": "KARTINA-1", "quantity": 1}],
            })
            db.job(con, "start_chat", 1, {"posting": "POSTING-1"})

        class UncertainAdapter:
            def __init__(self, *_args):
                pass

            def start(self, _posting):
                raise OzonError("read_timeout_unknown", unknown=True)

            def close(self):
                pass

        with patch("app.worker.OzonAdapter", UncertainAdapter):
            self.assertTrue(worker.tick())
        with db.transaction() as con:
            job = con.execute("SELECT state,error FROM jobs WHERE kind='start_chat'").fetchone()
            account = con.execute("SELECT error FROM accounts WHERE id=1").fetchone()
            sync = con.execute("SELECT failures,blocked_until FROM sync_state WHERE account_id=1").fetchone()
            self.assertEqual(tuple(job), ("unknown", "read_timeout_unknown"))
            self.assertIsNone(account["error"])
            self.assertTrue(worker._account_can_start(con, 1))
            self.assertTrue(sync is None or (sync["failures"] == 0 and sync["blocked_until"] is None))

    def test_adapter_uses_current_v4_fbs_posting_contract(self):
        with db.transaction() as con:
            account = dict(con.execute("SELECT * FROM accounts").fetchone())
        paths = []

        def handler(request):
            paths.append(request.url.path)
            payload = json.loads(request.content)
            self.assertEqual(payload["sort_dir"], "ASC")
            self.assertEqual(payload["cursor"], "")
            self.assertNotIn("offset", payload)
            return httpx.Response(
                200,
                json={
                    "result": {
                        "postings": [
                            {
                                "posting_number": "FBS-1",
                                "order_number": "FBS-ORDER",
                                "status": "awaiting_packaging",
                                "products": [],
                            }
                        ],
                        "has_next": False,
                        "cursor": "",
                    }
                },
            )

        adapter = OzonAdapter(account, self.settings, httpx.MockTransport(handler))
        self.assertEqual(len(list(adapter.orders("2026-09-01", "2026-09-22"))), 1)
        self.assertEqual(paths, ["/v4/posting/fbs/list"])
        adapter.close()

    def test_probe_is_diagnostic_and_does_not_require_legacy_http_fields(self):
        with db.transaction() as con:
            account = dict(con.execute("SELECT * FROM accounts").fetchone())
        paths = []

        def handler(request):
            paths.append(request.url.path)
            if request.url.path == "/v1/seller/info":
                return httpx.Response(200, json={"name": "Test seller"})
            if request.url.path == "/v4/posting/fbs/list":
                return httpx.Response(200, json={"postings": [], "has_next": False})
            if request.url.path == "/v3/chat/list":
                return httpx.Response(200, json={"chats": [], "has_next": False})
            return httpx.Response(404, json={})

        adapter = OzonAdapter(account, {}, httpx.MockTransport(handler))
        worker.probe_job({"account_id": 1}, adapter)
        adapter.close()
        self.assertEqual(
            paths,
            ["/v1/seller/info", "/v4/posting/fbs/list", "/v3/chat/list"],
        )
        with db.transaction() as con:
            capabilities = json.loads(
                con.execute("SELECT capabilities FROM accounts WHERE id=1").fetchone()[0]
            )
        self.assertTrue(capabilities["account"])
        self.assertTrue(capabilities["orders"])
        self.assertTrue(capabilities["list"])
        self.assertNotIn("history", capabilities)

    def test_probe_keeps_partial_capabilities_and_reports_failed_category(self):
        with db.transaction() as con:
            account = dict(con.execute("SELECT * FROM accounts").fetchone())

        def handler(request):
            if request.url.path == "/v1/seller/info":
                return httpx.Response(200, json={"name": "Test seller"})
            if request.url.path == "/v4/posting/fbs/list":
                return httpx.Response(200, json={"postings": []})
            if request.url.path == "/v3/chat/list":
                return httpx.Response(403, json={})
            return httpx.Response(404, json={})

        adapter = OzonAdapter(account, {}, httpx.MockTransport(handler))
        error = worker.probe_job({"account_id": 1}, adapter)
        adapter.close()
        self.assertEqual(error, "ozon_http_403")
        with db.transaction() as con:
            row = con.execute(
                "SELECT capabilities,error FROM accounts WHERE id=1"
            ).fetchone()
            capabilities = json.loads(row["capabilities"])
        self.assertTrue(capabilities["account"])
        self.assertTrue(capabilities["orders"])
        self.assertFalse(capabilities["list"])
        self.assertEqual(row["error"], "ozon_http_403")

    def test_sync_is_rejected_before_queue_when_import_date_is_missing(self):
        with db.transaction() as con:
            settings = db.config(con)
            settings.pop("sync_since", None)
            con.execute("UPDATE settings SET value=? WHERE id=1", (db.dump(settings),))
        response = self.client.post(
            "/accounts/1/sync",
            headers=self.admin,
            data={"csrf": "csrf"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("дату начала импорта", response.text)
        with db.transaction() as con:
            self.assertFalse(con.execute("SELECT 1 FROM jobs").fetchone())

    def test_legacy_transport_codes_are_human_readable_and_fields_are_removed(self):
        with db.transaction() as con:
            settings = db.config(con)
            settings.update(
                media_max_bytes=1,
                message_max_chars=2,
                media_hosts=["example.ozon.ru"],
            )
            con.execute("UPDATE settings SET value=? WHERE id=1", (db.dump(settings),))
            con.execute(
                "INSERT INTO jobs(kind,account_id,state,error,created_at,finished_at) VALUES('probe',1,'failed','configure_http_limits',?,?)",
                (db.now(), db.now()),
            )
        db.initialize()
        with db.transaction() as con:
            self.assertEqual(
                con.execute("SELECT error FROM jobs").fetchone()[0],
                "legacy_probe_configuration",
            )
            clean_settings = db.config(con)
            self.assertNotIn("media_max_bytes", clean_settings)
            self.assertNotIn("message_max_chars", clean_settings)
            self.assertNotIn("media_hosts", clean_settings)
        audit = self.client.get("/audit", headers=self.admin)
        self.assertIn("Операция создана старой версией Folio", audit.text)
        self.assertNotIn(">legacy_probe_configuration<", audit.text)
        automation = self.client.get("/settings/automation", headers=self.admin)
        self.assertNotIn("http_timeout", automation.text)
        self.assertNotIn("request_interval", automation.text)

    def test_settings_use_datetime_control_and_hide_transport_internals(self):
        response = self.client.get("/settings/ozon", headers=self.admin)
        self.assertIn('type="datetime-local"', response.text)
        self.assertNotIn("Подтверждённый лимит текста", response.text)
        self.assertNotIn("Максимальный размер изображения", response.text)
        self.assertNotIn("Подтверждённые хосты", response.text)

    def test_seller_article_selects_scenario_not_ozon_sku(self):
        with db.transaction() as con:
            scenario_ids = []
            for sku, kind in (("ARTICLE-A", "portrait_background"), ("ARTICLE-B", "collage")):
                sid = con.execute(
                    "INSERT INTO scenarios(name,product_type,draft) VALUES(?,?,?)",
                    (sku, kind, db.dump(graph(kind))),
                ).lastrowid
                scenarios.publish(con, sid, "1")
                con.execute(
                    "INSERT INTO mappings(account_id,sku,product_type,scenario_id) VALUES(1,?,?,?)",
                    (sku, kind, sid),
                )
                scenario_ids.append(sid)
            worker.import_order(
                con,
                1,
                {
                    "posting_number": "TWO-SKUS",
                    "status": "awaiting_packaging",
                    "products": [
                        {"sku": "OZON-A", "offer_id": "ARTICLE-A", "quantity": 1},
                        {"sku": "OZON-B", "offer_id": "ARTICLE-B", "quantity": 1},
                    ],
                },
            )
            rows = con.execute(
                "SELECT i.offer_id,m.product_type FROM items i JOIN mappings m ON m.id=i.mapping_id ORDER BY i.offer_id"
            ).fetchall()
        self.assertEqual(
            [(row["offer_id"], row["product_type"]) for row in rows],
            [("ARTICLE-A", "portrait_background"), ("ARTICLE-B", "collage")],
        )

    def test_catalog_sync_and_article_mapping_form(self):
        class CatalogAdapter:
            def products(self):
                yield {"offer_id": "ART-100", "sku": 700100, "product_id": 501}
                yield {"offer_id": "ДРУГОЙ-200", "sku": 700200, "product_id": 502}

        worker.sync_job(
            {"kind": "catalog_sync", "account_id": 1, "payload": "{}"},
            CatalogAdapter(),
            {},
        )
        with db.transaction() as con:
            sid = con.execute(
                "INSERT INTO scenarios(name,product_type,draft) VALUES('Art','portrait_background',?)",
                (db.dump(graph("portrait_background")),),
            ).lastrowid
            scenarios.publish(con, sid, "1")
        response = self.client.get("/catalog/products?account_id=1", headers=self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["products"], ["ART-100", "ДРУГОЙ-200"])
        self.assertEqual(
            self.client.get("/catalog/products?account_id=1", headers=self.manager).status_code,
            403,
        )
        bad = self.client.post(
            "/mappings", headers=self.admin,
            data={"csrf": "csrf", "account_id": 1, "sku": "700100", "scenario_id": sid},
        )
        self.assertEqual(bad.status_code, 422)
        saved = self.client.post(
            "/mappings", headers=self.admin,
            data={"csrf": "csrf", "account_id": 1, "sku": "ART-100", "scenario_id": sid},
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 303)
        with db.transaction() as con:
            worker.import_order(con, 1, {
                "posting_number": "ART-ORDER", "status": "new",
                "products": [{"sku": 700100, "offer_id": "ART-100", "quantity": 1}],
            })
            row = con.execute(
                "SELECT i.mapping_id,m.sku FROM items i JOIN mappings m ON m.id=i.mapping_id "
                "WHERE i.posting='ART-ORDER'"
            ).fetchone()
        self.assertEqual(row["sku"], "ART-100")

    def test_mapping_can_change_scenario_and_be_removed_without_losing_chat(self):
        instance_id, item_id, chat_id, old_scenario_id = self.setup_instance()
        with db.transaction() as con:
            mapping = con.execute("SELECT id,sku FROM mappings WHERE scenario_id=?", (old_scenario_id,)).fetchone()
            mapping_id = mapping["id"]
            article = mapping["sku"]
            old_version = con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0]
            con.execute("INSERT INTO ozon_products(account_id,offer_id) VALUES(1,?)", (article,))
            new_scenario_id = con.execute(
                "INSERT INTO scenarios(name,product_type,draft) VALUES('New scenario','collage',?)",
                (db.dump(graph("collage")),),
            ).lastrowid
            scenarios.publish(con, new_scenario_id, "1")

        page = self.client.get("/catalog", headers=self.admin)
        self.assertEqual(page.status_code, 200)
        self.assertIn(f"/mappings/{mapping_id}/edit", page.text)
        self.assertIn(f"/mappings/{mapping_id}/delete", page.text)
        self.assertEqual(
            self.client.post(f"/mappings/{mapping_id}/edit", headers=self.manager,
                             data={"csrf": "csrf", "scenario_id": new_scenario_id}).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(f"/mappings/{mapping_id}/edit", headers=self.admin,
                             data={"scenario_id": new_scenario_id}).status_code,
            403,
        )
        with db.transaction() as con:
            running_job = con.execute(
                "INSERT INTO jobs(kind,account_id,payload,state,created_at) "
                "VALUES('start_chat',1,?,'running',?)",
                (db.dump({"posting": "ORDER"}), db.now()),
            ).lastrowid
        self.assertEqual(
            self.client.post(f"/mappings/{mapping_id}/edit", headers=self.admin,
                             data={"csrf": "csrf", "scenario_id": new_scenario_id}).status_code,
            422,
        )
        with db.transaction() as con:
            con.execute("UPDATE jobs SET state='done' WHERE id=?", (running_job,))
        changed = self.client.post(
            f"/mappings/{mapping_id}/edit", headers=self.admin,
            data={"csrf": "csrf", "scenario_id": new_scenario_id},
            follow_redirects=False,
        )
        self.assertEqual(changed.status_code, 303)
        with db.transaction() as con:
            row = con.execute("SELECT scenario_id,product_type FROM mappings WHERE id=?", (mapping_id,)).fetchone()
            self.assertEqual((row["scenario_id"], row["product_type"]), (new_scenario_id, "collage"))
            self.assertEqual(con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0], old_version)

        self.assertEqual(
            self.client.post(f"/mappings/{mapping_id}/delete", headers=self.manager,
                             data={"csrf": "csrf", "confirm": "1"}).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(f"/mappings/{mapping_id}/delete", headers=self.admin,
                             data={"csrf": "csrf"}).status_code,
            422,
        )
        removed = self.client.post(
            f"/mappings/{mapping_id}/delete", headers=self.admin,
            data={"csrf": "csrf", "confirm": "1"}, follow_redirects=False,
        )
        self.assertEqual(removed.status_code, 303)
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT active FROM mappings WHERE id=?", (mapping_id,)).fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT mapping_id FROM items WHERE id=?", (item_id,)).fetchone()[0], mapping_id)
            self.assertEqual(con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0], old_version)
            worker.import_order(con, 1, {
                "posting_number": "LATER-ORDER", "status": "awaiting_packaging",
                "products": [{"sku": "42", "offer_id": article, "quantity": 1}],
            })
            self.assertIsNone(con.execute("SELECT mapping_id FROM items WHERE posting='LATER-ORDER'").fetchone()[0])
        self.assertNotIn(f"/mappings/{mapping_id}/edit", self.client.get("/catalog", headers=self.admin).text)

        restored = self.client.post(
            "/mappings", headers=self.admin,
            data={"csrf": "csrf", "account_id": 1, "sku": article, "scenario_id": new_scenario_id},
            follow_redirects=False,
        )
        self.assertEqual(restored.status_code, 303)
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT active FROM mappings WHERE id=?", (mapping_id,)).fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT mapping_id FROM items WHERE posting='LATER-ORDER'").fetchone()[0], mapping_id)

    def test_catalog_failure_keeps_last_complete_snapshot(self):
        with db.transaction() as con:
            con.execute(
                "INSERT INTO ozon_products(account_id,offer_id) VALUES(1,'EXISTING')"
            )

        class BrokenAdapter:
            def products(self):
                yield {"offer_id": "NEW"}
                raise OzonError("product_cursor_stalled")

        with self.assertRaises(OzonError):
            worker.sync_job(
                {"kind": "catalog_sync", "account_id": 1, "payload": "{}"},
                BrokenAdapter(),
                {},
            )
        with db.transaction() as con:
            self.assertEqual(
                [row[0] for row in con.execute("SELECT offer_id FROM ozon_products")],
                ["EXISTING"],
            )

    def test_legacy_offer_mapping_migrates_only_unambiguous_sku(self):
        new_mapping = (
            "CREATE TABLE IF NOT EXISTS mappings(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), sku TEXT NOT NULL, product_type TEXT NOT NULL, scenario_id INTEGER NOT NULL REFERENCES scenarios(id), template_ids TEXT NOT NULL DEFAULT '[]', UNIQUE(account_id,sku));"
        )
        old_mapping = (
            "CREATE TABLE IF NOT EXISTS mappings(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), offer_id TEXT NOT NULL, product_type TEXT NOT NULL, scenario_id INTEGER NOT NULL REFERENCES scenarios(id), template_ids TEXT NOT NULL DEFAULT '[]', UNIQUE(account_id,offer_id));"
        )
        with tempfile.TemporaryDirectory() as folder:
            previous = db.DATA
            db.DATA = Path(folder)
            try:
                con = sqlite3.connect(db.DATA / "folio.db")
                con.executescript(db.SCHEMA.replace(new_mapping, old_mapping))
                con.execute(
                    "INSERT INTO accounts(id,name,client_id,secret) VALUES(1,'Legacy','1','secret')"
                )
                con.execute(
                    "INSERT INTO scenarios(id,name,product_type,draft) VALUES(1,'Legacy','portrait_background','{}')"
                )
                con.execute(
                    "INSERT INTO mappings(id,account_id,offer_id,product_type,scenario_id) VALUES(1,1,'OFFER','portrait_background',1)"
                )
                con.execute(
                    "INSERT INTO items(id,account_id,posting,sku,offer_id,name,quantity,external_status,mapping_id) VALUES(1,1,'ORDER','SKU-1','OFFER','Item',1,'new',1)"
                )
                con.commit()
                con.close()
                db.initialize()
                with db.transaction() as migrated:
                    mapping = migrated.execute("SELECT sku FROM mappings").fetchone()
                    item = migrated.execute("SELECT mapping_id FROM items").fetchone()
                    item_fk = migrated.execute(
                        "PRAGMA foreign_key_list(items)"
                    ).fetchall()
                self.assertEqual(mapping["sku"], "OFFER")
                self.assertEqual(item["mapping_id"], 1)
                self.assertIn("mappings", [row[2] for row in item_fk])
                self.assertNotIn("mappings_offer_legacy", [row[2] for row in item_fk])
            finally:
                db.DATA = previous

    def test_existing_numeric_mappings_migrate_only_with_proven_article(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = db.DATA
            db.DATA = Path(folder)
            try:
                con = sqlite3.connect(db.DATA / "folio.db")
                con.executescript(db.SCHEMA)
                con.execute("INSERT INTO accounts(id,name,client_id,secret) VALUES(1,'Old','1','secret')")
                con.execute(
                    "INSERT INTO scenarios(id,name,product_type,draft) "
                    "VALUES(1,'Old','portrait_background','{}')"
                )
                con.executemany(
                    "INSERT INTO mappings(id,account_id,sku,product_type,scenario_id) "
                    "VALUES(?,1,?,'portrait_background',1)",
                    [(1, "123"), (2, "456")],
                )
                con.executemany(
                    "INSERT INTO items(account_id,posting,sku,offer_id,name,quantity,external_status,mapping_id) "
                    "VALUES(1,?,?,?,?,1,'new',?)",
                    [
                        ("P1", "123", "SELLER-123", "Painting", 1),
                        ("P2", "456", "FIRST-456", "Painting", 2),
                        ("P3", "456", "SECOND-456", "Painting", 2),
                    ],
                )
                con.commit()
                con.close()
                db.initialize()
                db.initialize()
                with db.transaction() as migrated:
                    rows = migrated.execute(
                        "SELECT id,sku,key_kind FROM mappings ORDER BY id"
                    ).fetchall()
                    item_links = migrated.execute(
                        "SELECT posting,mapping_id FROM items ORDER BY posting"
                    ).fetchall()
                self.assertEqual(
                    [(r["id"], r["sku"], r["key_kind"]) for r in rows],
                    [(1, "SELLER-123", "seller_article"), (2, "456", "ozon_sku")],
                )
                self.assertEqual(
                    [(r["posting"], r["mapping_id"]) for r in item_links],
                    [("P1", 1), ("P2", None), ("P3", None)],
                )
            finally:
                db.DATA = previous

    def test_repair_legacy_mapping_fk_preserves_existing_briefs(self):
        instance_id, item_id, chat_id, _ = self.setup_instance()
        raw = sqlite3.connect(db.DATA / "folio.db")
        try:
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute("ALTER TABLE mappings RENAME TO mappings_offer_legacy")
            raw.execute(
                "CREATE TABLE mappings(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), sku TEXT NOT NULL, product_type TEXT NOT NULL, scenario_id INTEGER NOT NULL REFERENCES scenarios(id), template_ids TEXT NOT NULL DEFAULT '[]', key_kind TEXT NOT NULL DEFAULT 'seller_article', UNIQUE(account_id,sku))"
            )
            raw.execute(
                "INSERT INTO mappings(id,account_id,sku,product_type,scenario_id,template_ids,key_kind) "
                "SELECT id,account_id,sku,product_type,scenario_id,template_ids,key_kind "
                "FROM mappings_offer_legacy"
            )
            raw.execute("DROP TABLE mappings_offer_legacy")
            raw.commit()
        finally:
            raw.close()

        db.initialize()
        db.initialize()
        with db.transaction() as con:
            self.assertEqual(
                con.execute("SELECT mapping_id FROM items WHERE id=?", (item_id,))
                .fetchone()[0],
                1,
            )
            self.assertEqual(
                con.execute(
                    "SELECT item_id FROM chat_items WHERE chat_id=?", (chat_id,)
                ).fetchone()[0],
                item_id,
            )
            self.assertEqual(
                con.execute("SELECT item_id FROM instances WHERE id=?", (instance_id,))
                .fetchone()[0],
                item_id,
            )
            self.assertIn(
                "mappings",
                [row[2] for row in con.execute("PRAGMA foreign_key_list(items)")],
            )
            worker.import_order(
                con,
                1,
                {
                    "posting_number": "AFTER-REPAIR",
                    "order_number": "ORDER-2",
                    "status": "awaiting_packaging",
                    "products": [
                        {
                            "sku": "1",
                            "offer_id": "offer1",
                            "name": "Painting",
                            "quantity": 1,
                        }
                    ],
                },
            )
            self.assertFalse(con.execute("PRAGMA foreign_key_check").fetchall())

    def test_catalog_and_builder_are_contextual(self):
        self.setup_instance()
        catalog = self.client.get("/catalog", headers=self.admin)
        self.assertIn("Артикулы и сценарии", catalog.text)
        self.assertIn('name="sku"', catalog.text)
        self.assertIn("артикул продавца", catalog.text)
        scenario = self.client.get("/scenarios/1", headers=self.admin)
        self.assertNotIn('data-field="kind"', scenario.text)
        self.assertIn('data-node-kind-help', scenario.text)
        self.assertIn('data-for="next"', scenario.text)
        self.assertNotIn("Варианты выбора", scenario.text)

    def test_visual_builder_drawer_and_split_settings_are_rendered(self):
        self.setup_instance()
        scenario = self.client.get("/scenarios/1", headers=self.admin)
        self.assertIn('data-scenario-builder', scenario.text)
        self.assertIn('data-graph-canvas', scenario.text)
        self.assertIn('data-inspector', scenario.text)
        inbox = self.client.get("/chats/1", headers=self.admin)
        self.assertIn('data-order-drawer', inbox.text)
        self.assertIn('has-active-chat', inbox.text)
        self.assertIn('folio-mobile-only', inbox.text)
        self.assertNotIn("<aside class=\"card card-body folio-stack\"><h2>Контекст", inbox.text)
        for section in ("ozon", "automation", "users"):
            self.assertEqual(
                self.client.get(f"/settings/{section}", headers=self.admin).status_code,
                200,
            )

    def test_scenario_preview_is_an_interactive_chat_started_by_a_new_order(self):
        _instance, _item, _chat, scenario_id = self.setup_instance()
        page = self.client.get(
            f"/scenarios/{scenario_id}/preview", headers=self.admin
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn("Поступил новый заказ", page.text)
        self.assertIn("Approved prompt photos", page.text)
        self.assertIn("Добавить тестовое фото", page.text)
        self.assertIn('name="message"', page.text)
        self.assertNotIn("по одному на строку", page.text)

        photo = self.client.post(
            f"/scenarios/{scenario_id}/preview",
            headers=self.admin,
            data={
                "csrf": "csrf",
                "history_json": "[]",
                "preview_action": "photo",
            },
        )
        self.assertEqual(photo.status_code, 200)
        self.assertIn("Добавлено тестовое фото", photo.text)
        self.assertIn("Approved prompt details", photo.text)

        finished = self.client.post(
            f"/scenarios/{scenario_id}/preview",
            headers=self.admin,
            data={
                "csrf": "csrf",
                "history_json": json.dumps([{"kind": "photo", "count": 1}]),
                "preview_action": "reply",
                "message": "Светлый фон",
            },
        )
        self.assertEqual(finished.status_code, 200)
        self.assertIn("Светлый фон", finished.text)
        self.assertIn("Бот завершил сбор данных", finished.text)

    def test_scenario_preview_applies_required_product_fields_automatically(self):
        value = graph("template_art")
        for node in value["nodes"]:
            if node.get("field") in {"photos", "template_id"}:
                node["required"] = False
        result = simulate(value, "template_art", ["photo:1", "1"])
        self.assertEqual(result["instance"]["status"], "needs_review")
        self.assertTrue(
            all(
                node.get("required")
                for node in value["nodes"]
                if node.get("field") in {"photos", "template_id"}
            )
        )

    def test_sent_message_shows_delivery_and_pinned_scenario_version(self):
        _iid, _item, chat_id, _scenario = self.setup_instance()

        class Adapter:
            def __init__(self, *args):
                pass

            def send(self, *_args):
                return "ozon-message-1"

            def close(self):
                pass

        worker.send_one(Adapter)
        page = self.client.get(f"/chats/{chat_id}", headers=self.admin)
        self.assertIn("Отправлено", page.text)
        self.assertIn("сценарий v1", page.text)
        self.assertIn("Approved prompt photos", page.text)

    def test_canvas_graph_json_persists_node_positions(self):
        _iid, _item, _chat, sid = self.setup_instance()
        with db.transaction() as con:
            row = con.execute("SELECT draft,revision FROM scenarios WHERE id=?", (sid,)).fetchone()
            value = json.loads(row["draft"])
            value["nodes"][0]["position"] = {"x": 444, "y": 222}
            revision = row["revision"]
        response = self.client.post(
            f"/scenarios/{sid}/save",
            headers=self.admin,
            data={"csrf": "csrf", "revision": revision, "graph_json": json.dumps(value)},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with db.transaction() as con:
            saved = json.loads(con.execute("SELECT draft FROM scenarios WHERE id=?", (sid,)).fetchone()[0])
        self.assertEqual(saved["nodes"][0]["position"], {"x": 444, "y": 222})

    def test_confirmation_node_is_optional_but_completeness_is_not(self):
        value = scenarios.initial_graph("portrait_background")
        for node in value["nodes"]:
            if node["kind"] in scenarios.TEXT_KINDS:
                node["text"] = "Approved prompt"
            if node["kind"] == "ask_photo":
                node.update(min=1, max=1)
        result = simulate(value, "portrait_background", ["photo:1", "Background"])
        self.assertEqual(result["instance"]["status"], "needs_review")

    def test_automation_activation_requires_confirmed_capabilities(self):
        response = self.client.post(
            "/settings/automation",
            headers=self.admin,
            data={
                "csrf": "csrf",
                "handoff_hours": "0",
                "timer_origin": "review",
                "manager_scope": "assigned",
                "send_enabled": "on",
                "auto_start_enabled": "on",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("подтвердите кабинет", response.text)

    def test_automation_settings_use_one_start_switch_without_status_filter(self):
        self.setup_instance()
        with db.transaction() as con:
            con.execute(
                "UPDATE accounts SET capabilities=?,checked_at=?,error=NULL WHERE id=1",
                (db.dump({"account": True, "orders": True, "list": True}), db.now()),
            )
        data = {
            "csrf": "csrf",
            "handoff_hours": "0",
            "timer_origin": "review",
            "manager_scope": "assigned",
            "send_enabled": "on",
            "auto_start_enabled": "on",
        }
        response = self.client.post(
            "/settings/automation", headers=self.admin, data=data,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with db.transaction() as con:
            saved = db.config(con)
        self.assertTrue(saved["start_enabled"])
        self.assertTrue(saved["automation_enabled"])
        self.assertTrue(saved["automation_enabled_at"])
        self.assertNotIn("start_statuses", saved)
        page = self.client.get("/settings/automation", headers=self.admin).text
        self.assertNotIn("Статусы Ozon для начала диалога", page)
        self.assertIn("Автоматически начинать чат и сценарий", page)
        self.assertIn("не перевод заказа Ozon или RetailCRM в печать", page)

    def test_legacy_automation_migration_keeps_switches_without_backlog(self):
        with db.transaction() as con:
            settings = {
                **self.settings,
                "start_enabled": True,
                "automation_enabled": True,
                "start_statuses": ["awaiting_packaging"],
            }
            con.execute("UPDATE settings SET value=? WHERE id=1", (db.dump(settings),))
        db.initialize()
        with db.transaction() as con:
            migrated = db.config(con)
        self.assertTrue(migrated["start_enabled"])
        self.assertTrue(migrated["automation_enabled"])
        self.assertTrue(migrated["automation_enabled_at"])
        self.assertNotIn("start_statuses", migrated)

    def test_unmatched_context_does_not_start_or_mix_items(self):
        _iid, _item, cid, _ = self.setup_instance()
        with db.transaction() as con:
            worker.import_order(
                con,
                1,
                {
                    "posting_number": "B",
                    "status": "created",
                    "products": [{"sku": "2", "offer_id": "unknown", "quantity": 1}],
                },
            )
            other = con.execute("SELECT id FROM items WHERE posting='B'").fetchone()[0]
            with self.assertRaises(scenarios.Invalid):
                scenarios.start(con, other, cid)
            con.execute("INSERT INTO assignments VALUES(?,'2')", (cid,))
        response = self.client.post(
            f"/chats/{cid}/context",
            headers=self.manager,
            data={"csrf": "csrf", "item_id": other},
        )
        self.assertEqual(response.status_code, 404)

    def test_collage_multiple_messages_waits_for_completion(self):
        result = simulate(
            graph("collage"),
            "collage",
            ["photo:1", "photo:2", "photos_done", "Text", "yes"],
        )
        self.assertEqual(result["instance"]["status"], "needs_review")
        self.assertEqual(len(json.loads(result["instance"]["fields"])["photos"]), 3)

    def test_ready_cannot_bypass_confirmation(self):
        value = graph("portrait_background")
        value["nodes"][0].update(
            kind="condition", field="background", otherwise="ready"
        )
        value["nodes"].insert(0, {"id": "entry", "kind": "start", "next": "start"})
        with self.assertRaises(scenarios.Invalid):
            scenarios.validate(value, "portrait_background")

    def test_new_message_invalidates_approved_manual_brief(self):
        iid, _item, cid, _ = self.finish("portrait_background")
        with db.transaction() as con:
            scenarios.approve(con, iid, "2")
            scenarios.release_due(con)
            scenarios.takeover(con, cid, "2")
            chat = con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
            worker.import_message(
                con,
                chat,
                {
                    "message_id": "new",
                    "user": {"type": "Customer"},
                    "data": ["Changed wishes"],
                },
            )
            worker.process_event(con, con.execute("SELECT * FROM events").fetchone())
            self.assertEqual(
                con.execute("SELECT status FROM instances").fetchone()[0],
                "needs_manager",
            )

    def test_delay_and_pending_events_block_export(self):
        iid, _item, _cid, _ = self.finish("portrait_background")
        with db.transaction() as con:
            settings = dict(self.settings, handoff_hours=12)
            con.execute("UPDATE settings SET value=?", (db.dump(settings),))
            scenarios.approve(con, iid, "2")
            scenarios.release_due(con)
            self.assertEqual(
                con.execute("SELECT status FROM instances").fetchone()[0],
                "waiting_release",
            )
        self.assertEqual(
            self.client.get(f"/briefs/{iid}/export", headers=self.admin).status_code,
            422,
        )

    def test_revoked_manager_loses_access(self):
        with db.transaction() as con:
            con.execute("INSERT INTO revoked_users VALUES('2')")
        self.assertEqual(self.client.get("/", headers=self.manager).status_code, 403)

    def test_failed_event_does_not_poison_queue(self):
        _, _, cid, _ = self.setup_instance()
        with db.transaction() as con:
            con.execute("UPDATE outbox SET state='sent',external_id='sent-before-bad-event'")
            con.execute(
                "INSERT INTO events(account_id,external_key,chat_id,body) VALUES(1,'bad',?,'{}')",
                (cid,),
            )
        worker.tick()
        with db.transaction() as con:
            self.assertEqual(
                con.execute("SELECT state FROM events").fetchone()[0], "failed"
            )
            self.assertEqual(
                con.execute("SELECT mode FROM chats").fetchone()[0], "manual"
            )

    def test_secrets_saved_encrypted_and_rotation_invalidates_capabilities(self):
        response = self.client.post(
            "/accounts",
            headers=self.admin,
            data={
                "csrf": "csrf",
                "id": "1",
                "name": "Account",
                "client_id": "client",
                "api_key": "ROTATED_SECRET",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with db.transaction() as con:
            account = con.execute("SELECT * FROM accounts").fetchone()
            self.assertNotIn("ROTATED_SECRET", account["secret"])
            self.assertEqual(
                security.cipher().decrypt(account["secret"].encode()), b"ROTATED_SECRET"
            )
            self.assertEqual(account["capabilities"], "{}")
        self.assertNotIn(
            "ROTATED_SECRET", self.client.get("/settings", headers=self.admin).text
        )

    def test_renaming_account_preserves_verified_capabilities(self):
        with db.transaction() as con:
            con.execute(
                "UPDATE accounts SET capabilities=?,checked_at=? WHERE id=1",
                (db.dump({"account": True}), db.now()),
            )
        response = self.client.post(
            "/accounts",
            headers=self.admin,
            data={
                "csrf": "csrf",
                "id": "1",
                "name": "Новое локальное название",
                "client_id": "client",
                "api_key": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with db.transaction() as con:
            account = con.execute("SELECT * FROM accounts WHERE id=1").fetchone()
        self.assertEqual(account["name"], "Новое локальное название")
        self.assertEqual(json.loads(account["capabilities"]), {"account": True})
        self.assertIsNotNone(account["checked_at"])

    def retail_graph(self):
        value = graph("portrait_background")
        next(node for node in value["nodes"] if node["id"] == "details")[
            "next"
        ] = "retailcrm"
        value["nodes"].extend(
            [
                {
                    "id": "retailcrm",
                    "kind": "retailcrm",
                    "title": "Создать сделку",
                    "comment": "Заказ {posting}; SKU {sku}; {summary}",
                    "next": "ready",
                    "error": "retailcrm_error",
                },
                {"id": "retailcrm_error", "kind": "end", "title": "Ошибка CRM"},
            ]
        )
        return value

    def setup_retail_instance(self):
        value = self.retail_graph()
        with db.transaction() as con:
            scenario_id = con.execute(
                "INSERT INTO scenarios(name,product_type,draft) VALUES('Retail','portrait_background',?)",
                (db.dump(value),),
            ).lastrowid
            scenarios.publish(con, scenario_id, "1")
            con.execute(
                "INSERT INTO mappings(account_id,sku,product_type,scenario_id) "
                "VALUES(1,'retail-offer','portrait_background',?)",
                (scenario_id,),
            )
            worker.import_order(
                con,
                1,
                {
                    "posting_number": "OZON-RETAIL-1",
                    "status": "awaiting_packaging",
                    "products": [
                        {
                            "sku": "retail-sku",
                            "offer_id": "retail-offer",
                            "name": "Картина по фото",
                            "quantity": 1,
                        }
                    ],
                },
            )
            item_id = con.execute(
                "SELECT id FROM items WHERE sku='retail-sku'"
            ).fetchone()[0]
            chat = worker.import_chat(con, 1, {"chat_id": "RETAIL-CHAT"})
            con.execute("INSERT INTO chat_items VALUES(?,?)", (chat["id"], item_id))
            con.execute(
                "UPDATE chats SET mode='bot',active_item=? WHERE id=?",
                (item_id, chat["id"]),
            )
            scenarios.start(con, item_id, chat["id"])
            instance_id = con.execute(
                "SELECT id FROM instances WHERE item_id=?", (item_id,)
            ).fetchone()[0]
            scenarios.advance(con, instance_id)
            media_id = self.image(con, chat["id"], item_id)
            scenarios.advance(con, instance_id, {"media_ids": [media_id]})
            scenarios.advance(con, instance_id, {"text": "Светлый фон"})
            job = con.execute(
                "SELECT * FROM jobs WHERE kind='retailcrm_create'"
            ).fetchone()
        return instance_id, job

    def test_retailcrm_settings_encrypt_key_and_require_admin(self):
        response = self.client.post(
            "/settings/retailcrm",
            headers=self.admin,
            data={
                "csrf": "csrf",
                "base_url": "https://shop.retailcrm.ru",
                "site": "bellenne",
                "api_key": "RETAIL_PRIVATE_KEY",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with db.transaction() as con:
            record = con.execute("SELECT * FROM retailcrm_integrations").fetchone()
            self.assertNotIn("RETAIL_PRIVATE_KEY", record["secret"])
            self.assertEqual(
                security.cipher().decrypt(record["secret"].encode()),
                b"RETAIL_PRIVATE_KEY",
            )
        page = self.client.get("/settings/retailcrm", headers=self.admin)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Проверить подключение и права", page.text)
        self.assertNotIn("RETAIL_PRIVATE_KEY", page.text)
        self.assertEqual(
            self.client.get("/settings/retailcrm", headers=self.manager).status_code,
            403,
        )

    def test_retailcrm_adapter_uses_v5_contract_and_api_key_parameter(self):
        from urllib.parse import parse_qs

        requests = []

        def handler(request):
            requests.append(request)
            if request.method == "GET":
                self.assertEqual(request.url.params["apiKey"], "RETAIL_KEY")
            if request.url.path == "/api/credentials":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "scopes": ["order_read", "order_write"],
                        "sitesAvailable": ["bellenne"],
                    },
                )
            if request.method == "GET":
                return httpx.Response(404, json={"success": False})
            body = parse_qs(request.content.decode())
            self.assertEqual(body["site"], ["bellenne"])
            self.assertEqual(body["apiKey"], ["RETAIL_KEY"])
            order = json.loads(body["order"][0])
            self.assertEqual(order["externalId"], "folio_instance_7")
            return httpx.Response(201, json={"success": True, "id": 44})

        with db.transaction() as con:
            con.execute(
                "INSERT INTO retailcrm_integrations(id,base_url,site,secret) VALUES(1,?,?,?)",
                (
                    "https://shop.retailcrm.ru",
                    "bellenne",
                    security.cipher().encrypt(b"RETAIL_KEY").decode(),
                ),
            )
            record = con.execute("SELECT * FROM retailcrm_integrations").fetchone()
        adapter = retailcrm.RetailCRMAdapter(record, transport=httpx.MockTransport(handler))
        try:
            self.assertEqual(
                adapter.credentials(),
                {"order_read": True, "order_write": True, "reference_read": False, "site": True},
            )
            self.assertIsNone(adapter.find_order("folio_instance_7"))
            self.assertEqual(
                adapter.create_order(
                    {"externalId": "folio_instance_7", "managerComment": "Бриф"}
                )["id"],
                44,
            )
        finally:
            adapter.close()
        self.assertEqual([request.method for request in requests], ["GET", "GET", "POST"])

    def test_retailcrm_comment_edit_keeps_all_order_items(self):
        from urllib.parse import parse_qs

        with db.transaction() as con:
            con.execute(
                "INSERT INTO retailcrm_integrations(id,base_url,site,secret) VALUES(1,?,?,?)",
                ("https://shop.retailcrm.ru", "bellenne",
                 security.cipher().encrypt(b"RETAIL_KEY").decode()),
            )
            record = con.execute("SELECT * FROM retailcrm_integrations").fetchone()
        requests = []

        def handler(request):
            requests.append(request)
            self.assertEqual(request.url.path, "/api/v5/orders/folio_instance_7/edit")
            self.assertEqual(request.method, "POST")
            body = parse_qs(request.content.decode())
            self.assertEqual(body["apiKey"], ["RETAIL_KEY"])
            self.assertEqual(body["by"], ["externalId"])
            self.assertEqual(body["site"], ["bellenne"])
            self.assertEqual(json.loads(body["order"][0]), {
                "customerComment": "Макет подтверждён покупателем",
                "items": [{"id": 11}, {"id": 12}],
            })
            return httpx.Response(200, json={"success": True})

        adapter = retailcrm.RetailCRMAdapter(record, transport=httpx.MockTransport(handler))
        try:
            adapter.edit_order_customer_comment("folio_instance_7", "Макет подтверждён покупателем",
                                                [{"id": 11}, {"id": 12}])
        finally:
            adapter.close()
        self.assertEqual(len(requests), 1)

    def test_retailcrm_summary_links_to_every_private_photo(self):
        instance_id, item_id, chat_id, _ = self.setup_instance("collage")
        with db.transaction() as con:
            first = self.image(con, chat_id, item_id)
            second = self.image(con, chat_id, item_id)
            con.execute(
                "UPDATE instances SET fields=? WHERE id=?",
                (db.dump({"photos": [first, second], "caption": "Подарок",
                          "wishes": "Без согласования"}), instance_id),
            )
            with patch.dict(
                os.environ,
                {
                    "FOLIO_PUBLIC_BASE_URL": "https://one.example.ru",
                    "MODULE_PREFIX": "/folio",
                },
            ):
                external_id, order = retailcrm.order_payload(
                    con, instance_id, {"comment": "{summary}"}
                )
        self.assertEqual(external_id, f"folio_instance_{instance_id}")
        self.assertEqual(
            order["managerComment"],
            "Фото: 1. https://one.example.ru/folio/media/"
            f"{first}\n2. https://one.example.ru/folio/media/{second}"
            "\nНадпись: Подарок\nСогласование: Без согласования",
        )
        self.assertNotIn("Фото: 2\n", order["managerComment"])

    def test_retailcrm_photo_link_requires_configured_base_and_stored_media(self):
        instance_id, item_id, chat_id, _ = self.setup_instance()
        with db.transaction() as con:
            mid = self.image(con, chat_id, item_id)
            con.execute(
                "UPDATE instances SET fields=? WHERE id=?",
                (db.dump({"photos": [mid]}), instance_id),
            )
            with patch.dict(os.environ, {"FOLIO_PUBLIC_BASE_URL": ""}):
                _, plain_order = retailcrm.order_payload(
                    con, instance_id, {"comment": "Создать без ссылок"}
                )
                self.assertEqual(plain_order["managerComment"], "Создать без ссылок")
                with self.assertRaisesRegex(
                    retailcrm.RetailCRMError, "retailcrm_public_base_url_missing"
                ):
                    retailcrm.order_payload(con, instance_id, {"comment": "{summary}"})
            with patch.dict(
                os.environ, {"FOLIO_PUBLIC_BASE_URL": "http://one.example.ru"}
            ):
                with self.assertRaisesRegex(
                    retailcrm.RetailCRMError, "retailcrm_public_base_url_invalid"
                ):
                    retailcrm.order_payload(con, instance_id, {"comment": "{summary}"})
            with patch.dict(
                os.environ, {"FOLIO_PUBLIC_BASE_URL": "https://one.example.ru"}
            ):
                con.execute("UPDATE media SET item_id=NULL WHERE id=?", (mid,))
                with self.assertRaisesRegex(
                    retailcrm.RetailCRMError, "retailcrm_media_missing"
                ):
                    retailcrm.order_payload(con, instance_id, {"comment": "{summary}"})
                con.execute("UPDATE media SET item_id=? WHERE id=?", (item_id, mid))
                _, order = retailcrm.order_payload(
                    con, instance_id, {"comment": "{summary}"}
                )
                self.assertEqual(
                    order["managerComment"],
                    f"Фото: https://one.example.ru/folio/media/{mid}",
                )

    def test_retailcrm_node_simulates_and_requires_error_branch(self):
        value = self.retail_graph()
        scenarios.validate(value, "portrait_background")
        result = simulate(value, "portrait_background", ["photo:1", "Фон"])
        self.assertEqual(result["instance"]["status"], "needs_review")
        retail_node = next(
            node for node in value["nodes"] if node["kind"] == "retailcrm"
        )
        retail_node["error"] = ""
        with self.assertRaisesRegex(scenarios.Invalid, "переход при ошибке"):
            scenarios.validate(value, "portrait_background")

    def test_handoff_cannot_hide_retailcrm_action_after_manual_takeover(self):
        value = self.retail_graph()
        retail_node = next(n for n in value["nodes"] if n["kind"] == "retailcrm")
        retail_node["error"] = "handoff"
        value["nodes"].append(
            {
                "id": "handoff",
                "kind": "handoff",
                "title": "Передать менеджеру",
                "next": "ready",
            }
        )
        with self.assertRaisesRegex(scenarios.Invalid, "останавливает автоматический"):
            scenarios.validate(value, "portrait_background")
        value["nodes"][-1]["next"] = "retailcrm_error"
        scenarios.validate(value, "portrait_background")

    def test_retailcrm_node_on_photo_error_branch_runs_before_handoff(self):
        value = self.retail_graph()
        next(n for n in value["nodes"] if n["kind"] == "ask_photo")[
            "error"
        ] = "error_retailcrm"
        value["nodes"].extend(
            [
                {
                    "id": "error_retailcrm",
                    "kind": "retailcrm",
                    "title": "Сделка при ошибке фото",
                    "comment": "Заказ {posting}: требуется менеджер",
                    "next": "handoff",
                    "error": "handoff",
                },
                {
                    "id": "handoff",
                    "kind": "handoff",
                    "title": "Передать менеджеру",
                    "next": "retailcrm_error",
                },
            ]
        )
        scenarios.validate(value, "portrait_background")
        with db.transaction() as con:
            scenario_id = con.execute(
                "INSERT INTO scenarios(name,product_type,draft) VALUES('Error CRM','portrait_background',?)",
                (db.dump(value),),
            ).lastrowid
            scenarios.publish(con, scenario_id, "1")
            con.execute(
                "INSERT INTO mappings(account_id,sku,product_type,scenario_id) "
                "VALUES(1,'error-crm-offer','portrait_background',?)",
                (scenario_id,),
            )
            worker.import_order(
                con,
                1,
                {
                    "posting_number": "OZON-ERROR-CRM-1",
                    "status": "awaiting_packaging",
                    "products": [
                        {
                            "sku": "error-crm-sku",
                            "offer_id": "error-crm-offer",
                            "name": "Картина по фото",
                            "quantity": 1,
                        }
                    ],
                },
            )
            item_id = con.execute(
                "SELECT id FROM items WHERE sku='error-crm-sku'"
            ).fetchone()[0]
            chat = worker.import_chat(con, 1, {"chat_id": "ERROR-CRM-CHAT"})
            con.execute("INSERT INTO chat_items VALUES(?,?)", (chat["id"], item_id))
            con.execute(
                "UPDATE chats SET mode='bot',active_item=? WHERE id=?",
                (item_id, chat["id"]),
            )
            scenarios.start(con, item_id, chat["id"])
            instance_id = con.execute(
                "SELECT id FROM instances WHERE item_id=?", (item_id,)
            ).fetchone()[0]
            scenarios.advance(con, instance_id)
            scenarios.advance(con, instance_id, {"text": "Фото не приложено"})
            action = con.execute(
                "SELECT * FROM retailcrm_actions WHERE instance_id=?", (instance_id,)
            ).fetchone()
            job = con.execute(
                "SELECT * FROM jobs WHERE kind='retailcrm_create'"
            ).fetchone()
            instance = con.execute(
                "SELECT * FROM instances WHERE id=?", (instance_id,)
            ).fetchone()
        self.assertEqual(action["node_id"], "error_retailcrm")
        self.assertIsNotNone(job)
        self.assertEqual(instance["status"], "waiting_integration")
        worker.finalize_retailcrm_action(action["id"], "sent", retailcrm_id=42)
        with db.transaction() as con:
            instance = con.execute(
                "SELECT * FROM instances WHERE id=?", (instance_id,)
            ).fetchone()
            chat = con.execute("SELECT * FROM chats WHERE id=?", (chat["id"],)).fetchone()
        self.assertEqual(instance["status"], "needs_manager")
        self.assertEqual(chat["mode"], "manual")

    def test_retailcrm_job_reuses_existing_order_and_continues_scenario(self):
        with db.transaction() as con:
            con.execute(
                "INSERT INTO retailcrm_integrations(id,base_url,site,secret,capabilities,checked_at) "
                "VALUES(1,'https://shop.retailcrm.ru','bellenne',?,?,?)",
                (
                    security.cipher().encrypt(b"RETAIL_KEY").decode(),
                    db.dump({"order_read": True, "order_write": True, "site": True}),
                    db.now(),
                ),
            )
        instance_id, job = self.setup_retail_instance()

        class ExistingOrderAdapter:
            created = False

            def __init__(self, _record):
                pass

            def find_order(self, external_id):
                self.external_id = external_id
                return {"success": True, "order": {"id": 91}}

            def create_order(self, _order):
                self.created = True
                raise AssertionError("existing order must not be created again")

            def close(self):
                pass

        with patch.dict(os.environ, {"FOLIO_PUBLIC_BASE_URL": ""}):
            state, error = worker.retailcrm_create_job(job, ExistingOrderAdapter)
        self.assertEqual((state, error), ("sent", None))
        self.assertFalse(ExistingOrderAdapter.created)
        with db.transaction() as con:
            action = con.execute("SELECT * FROM retailcrm_actions").fetchone()
            instance = con.execute(
                "SELECT * FROM instances WHERE id=?", (instance_id,)
            ).fetchone()
        self.assertEqual(action["state"], "sent")
        self.assertEqual(action["retailcrm_id"], 91)
        self.assertEqual(instance["status"], "needs_review")

    def test_retailcrm_unknown_result_stops_bot_without_retry(self):
        with db.transaction() as con:
            con.execute(
                "INSERT INTO retailcrm_integrations(id,base_url,site,secret,capabilities,checked_at) "
                "VALUES(1,'https://shop.retailcrm.ru','bellenne',?,?,?)",
                (
                    security.cipher().encrypt(b"RETAIL_KEY").decode(),
                    db.dump({"order_read": True, "order_write": True, "site": True}),
                    db.now(),
                ),
            )
        instance_id, job = self.setup_retail_instance()

        class UnknownAdapter:
            def __init__(self, _record):
                pass

            def find_order(self, _external_id):
                raise retailcrm.RetailCRMError(
                    "retailcrm_transport_unknown", unknown=True
                )

            def close(self):
                pass

        state, error = worker.retailcrm_create_job(job, UnknownAdapter)
        self.assertEqual((state, error), ("unknown", "retailcrm_transport_unknown"))
        with db.transaction() as con:
            action = con.execute("SELECT * FROM retailcrm_actions").fetchone()
            instance = con.execute(
                "SELECT * FROM instances WHERE id=?", (instance_id,)
            ).fetchone()
            chat = con.execute(
                "SELECT * FROM chats WHERE id=?", (instance["chat_id"],)
            ).fetchone()
        self.assertEqual(action["state"], "unknown")
        self.assertEqual(instance["status"], "needs_manager")
        self.assertEqual(chat["mode"], "manual")

    def test_retailcrm_missing_photo_base_fails_before_external_create(self):
        with db.transaction() as con:
            con.execute(
                "INSERT INTO retailcrm_integrations(id,base_url,site,secret,capabilities,checked_at) "
                "VALUES(1,'https://shop.retailcrm.ru','bellenne',?,?,?)",
                (
                    security.cipher().encrypt(b"RETAIL_KEY").decode(),
                    db.dump({"order_read": True, "order_write": True, "site": True}),
                    db.now(),
                ),
            )
        instance_id, job = self.setup_retail_instance()

        class Adapter:
            def __init__(self, _record):
                pass

            def find_order(self, _external_id):
                return None

            def create_order(self, _order):
                raise AssertionError("No RetailCRM write without a working photo link")

            def close(self):
                pass

        with patch.dict(os.environ, {"FOLIO_PUBLIC_BASE_URL": ""}):
            state, error = worker.retailcrm_create_job(job, Adapter)
        self.assertEqual((state, error), ("failed", "retailcrm_public_base_url_missing"))
        with db.transaction() as con:
            action = con.execute("SELECT * FROM retailcrm_actions").fetchone()
            instance = con.execute(
                "SELECT * FROM instances WHERE id=?", (instance_id,)
            ).fetchone()
        self.assertEqual(action["state"], "failed")
        self.assertEqual(instance["status"], "closed")

    def test_retailcrm_definitive_rejection_uses_error_branch(self):
        with db.transaction() as con:
            con.execute(
                "INSERT INTO retailcrm_integrations(id,base_url,site,secret,capabilities,checked_at) "
                "VALUES(1,'https://shop.retailcrm.ru','bellenne',?,?,?)",
                (
                    security.cipher().encrypt(b"RETAIL_KEY").decode(),
                    db.dump({"order_read": True, "order_write": True, "site": True}),
                    db.now(),
                ),
            )
        instance_id, job = self.setup_retail_instance()

        class RejectedAdapter:
            def __init__(self, _record):
                pass

            def find_order(self, _external_id):
                return None

            def create_order(self, _order):
                raise retailcrm.RetailCRMError("retailcrm_http_400")

            def close(self):
                pass

        state, error = worker.retailcrm_create_job(job, RejectedAdapter)
        self.assertEqual((state, error), ("failed", "retailcrm_http_400"))
        with db.transaction() as con:
            action = con.execute("SELECT * FROM retailcrm_actions").fetchone()
            instance = con.execute(
                "SELECT * FROM instances WHERE id=?", (instance_id,)
            ).fetchone()
        self.assertEqual(action["state"], "failed")
        self.assertEqual(instance["status"], "closed")


    def test_reused_ozon_chat_starts_a_separate_order(self):
        first_instance, first_item, first_chat, sid = self.setup_instance(
            posting="POSTING-A", external_chat="SHARED-OZON-CHAT"
        )
        settings = {
            **self.settings, "start_enabled": True, "automation_enabled": True,
            "automation_enabled_at": "2026-09-23T00:00:00+00:00",
        }
        with db.transaction() as con:
            con.execute("UPDATE accounts SET capabilities=? WHERE id=1", (
                db.dump({"account": True, "orders": True, "list": True, "history": True}),
            ))
            offer = con.execute("SELECT sku FROM mappings WHERE scenario_id=?", (sid,)).fetchone()[0]
            worker.import_order(con, 1, {
                "posting_number": "POSTING-B", "in_process_at": "2026-09-24T08:00:00Z",
                "status": "awaiting_packaging",
                "products": [{"sku": str(sid), "offer_id": offer, "name": "Painting", "quantity": 1}],
            })
            second_item = con.execute("SELECT id FROM items WHERE posting='POSTING-B'").fetchone()[0]
            job_id = con.execute(
                "INSERT INTO jobs(kind,account_id,payload,created_at) VALUES('start_chat',1,?,?)",
                (db.dump({"posting": "POSTING-B"}), db.now()),
            ).lastrowid

        class Adapter:
            def start(self, posting):
                self.posting = posting
                return "SHARED-OZON-CHAT"

        adapter = Adapter()
        worker.sync_job(
            {"id": job_id, "kind": "start_chat", "account_id": 1,
             "payload": db.dump({"posting": "POSTING-B"})}, adapter, settings,
        )
        self.assertEqual(adapter.posting, "POSTING-B")
        with db.transaction() as con:
            chats = con.execute(
                "SELECT id,posting,active_item FROM chats WHERE external_id='SHARED-OZON-CHAT' ORDER BY id"
            ).fetchall()
            self.assertEqual([row["posting"] for row in chats], ["POSTING-A", "POSTING-B"])
            self.assertEqual([row["active_item"] for row in chats], [first_item, second_item])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM instances").fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM outbox WHERE state='pending'").fetchone()[0], 2)
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertNotEqual(first_chat, chats[1]["id"])
        with db.transaction() as con:
            worker.import_order(con, 1, {
                "posting_number": "POSTING-A", "status": "cancelled",
                "products": [{"sku": str(sid), "offer_id": offer, "name": "Painting", "quantity": 1}],
            })
            self.assertEqual(con.execute(
                "SELECT status FROM instances WHERE id=?", (first_instance,)
            ).fetchone()[0], "closed")
            self.assertEqual(con.execute(
                "SELECT status FROM instances WHERE item_id=?", (second_item,)
            ).fetchone()[0], "collecting")
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM outbox WHERE chat_id=? AND state='pending'",
                (chats[1]["id"],),
            ).fetchone()[0], 1)

    def test_shared_chat_reply_without_order_goes_to_manager(self):
        _, _, first_chat, _ = self.setup_instance(posting="POSTING-A", external_chat="SHARED")
        _, _, second_chat, _ = self.setup_instance(posting="POSTING-B", external_chat="SHARED")
        message = {
            "message_id": "reply-without-order", "created_at": db.now(),
            "user": {"type": "Customer"}, "data": ["Нужно изменить фон"],
        }

        class Adapter:
            def seller_info(self):
                return {}

            def order_pages(self, *_args):
                return iter([])

            def chats(self):
                return iter([{"chat": {"chat_id": "SHARED", "chat_type": "BUYER_SELLER"}}])

            def history(self, _chat_id):
                return iter([message])

        settings = {**self.settings, "sync_since": "2026-09-23T00:00:00+00:00"}
        for _ in range(2):
            worker.sync_job({"kind": "sync", "account_id": 1, "payload": "{}"}, Adapter(), settings)
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM messages WHERE external_id='reply-without-order'").fetchone()[0], 2)
            self.assertEqual([row[0] for row in con.execute(
                "SELECT mode FROM chats WHERE id IN (?,?) ORDER BY id", (first_chat, second_chat)
            )], ["manual", "manual"])
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM audit WHERE action='chat.ambiguous_order_reply'"
            ).fetchone()[0], 2)

    def test_shared_chat_explicit_order_reply_routes_only_that_order(self):
        _, _, first_chat, _ = self.setup_instance(posting="POSTING-A", external_chat="SHARED")
        _, _, second_chat, _ = self.setup_instance(posting="POSTING-B", external_chat="SHARED")
        with db.transaction() as con:
            con.execute("UPDATE items SET order_number=posting")
        message = {
            "message_id": "reply-for-b", "created_at": db.now(),
            "context": {"order_number": "POSTING-B"},
            "user": {"type": "Customer"}, "data": ["Да"],
        }

        class Adapter:
            def seller_info(self):
                return {}

            def order_pages(self, *_args):
                return iter([])

            def chats(self):
                return iter([{"chat": {"chat_id": "SHARED", "chat_type": "BUYER_SELLER"}}])

            def history(self, _chat_id):
                return iter([message])

        worker.sync_job(
            {"kind": "sync", "account_id": 1, "payload": "{}"}, Adapter(),
            {**self.settings, "sync_since": "2026-09-23T00:00:00+00:00"},
        )
        with db.transaction() as con:
            self.assertEqual([row[0] for row in con.execute(
                "SELECT chat_id FROM messages WHERE external_id='reply-for-b'"
            )], [second_chat])
            self.assertEqual([row[0] for row in con.execute("SELECT chat_id FROM events")], [second_chat])
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (first_chat,)).fetchone()[0], "bot")

    def test_shared_chat_reply_after_old_order_cancelled_routes_new_order(self):
        _, _, old_chat, sid = self.setup_instance(posting="OLD", external_chat="SHARED")
        _, _, new_chat, _ = self.setup_instance(posting="NEW", external_chat="SHARED")
        with db.transaction() as con:
            offer = con.execute("SELECT sku FROM mappings WHERE scenario_id=?", (sid,)).fetchone()[0]
            worker.import_order(con, 1, {
                "posting_number": "OLD", "status": "cancelled",
                "products": [{"sku": str(sid), "offer_id": offer, "name": "Painting", "quantity": 1}],
            })
        message = {
            "message_id": "new-order-reply", "created_at": db.now(),
            "user": {"type": "Customer"}, "data": ["Здравствуйте"],
        }

        class Adapter:
            def seller_info(self):
                return {}

            def order_pages(self, *_args):
                return iter([])

            def chats(self):
                return iter([{"chat": {"chat_id": "SHARED", "chat_type": "BUYER_SELLER"}}])

            def history(self, _chat_id):
                return iter([message])

        worker.sync_job(
            {"kind": "sync", "account_id": 1, "payload": "{}"}, Adapter(),
            {**self.settings, "sync_since": "2026-09-23T00:00:00+00:00"},
        )
        with db.transaction() as con:
            self.assertEqual([row[0] for row in con.execute(
                "SELECT chat_id FROM messages WHERE external_id='new-order-reply'"
            )], [new_chat])
            self.assertEqual([row[0] for row in con.execute("SELECT chat_id FROM events")], [new_chat])
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (old_chat,)).fetchone()[0], "bot")

    def test_legacy_chat_schema_preserves_links_and_recovers_only_known_reuse(self):
        _, item, chat_id, _ = self.setup_instance(posting="LEGACY-POSTING", external_chat="SHARED")
        con = db.connect()
        try:
            con.execute("PRAGMA foreign_keys=OFF")
            con.execute(
                "CREATE TABLE chats_legacy(id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL REFERENCES accounts(id),"
                "external_id TEXT NOT NULL,title TEXT NOT NULL,chat_type TEXT NOT NULL DEFAULT '',"
                "buyer_id TEXT NOT NULL DEFAULT '',buyer_name TEXT NOT NULL DEFAULT '',"
                "buyer_name_manual TEXT NOT NULL DEFAULT '',start_job_id INTEGER REFERENCES jobs(id),"
                "mode TEXT NOT NULL DEFAULT 'manual',epoch INTEGER NOT NULL DEFAULT 0,"
                "active_item INTEGER REFERENCES items(id),unread INTEGER NOT NULL DEFAULT 0,"
                "updated_at TEXT NOT NULL,UNIQUE(account_id,external_id))"
            )
            fields = "id,account_id,external_id,title,chat_type,buyer_id,buyer_name,buyer_name_manual,start_job_id,mode,epoch,active_item,unread,updated_at"
            con.execute(f"INSERT INTO chats_legacy({fields}) SELECT {fields} FROM chats")
            con.execute("DROP TABLE chats")
            con.execute("ALTER TABLE chats_legacy RENAME TO chats")
            con.execute(
                "INSERT INTO jobs(kind,account_id,payload,state,error,created_at) "
                "VALUES('start_chat',1,?,'unknown','start_returned_existing_chat',?)",
                (db.dump({"posting": "NEXT-POSTING"}), db.now()),
            )
            con.execute(
                "INSERT INTO jobs(kind,account_id,payload,state,error,created_at) "
                "VALUES('start_chat',1,?,'unknown','transport_unknown',?)",
                (db.dump({"posting": "UNCERTAIN-POSTING"}), db.now()),
            )
            con.commit()
        finally:
            con.close()
        db.initialize()
        with db.transaction() as con:
            old = con.execute("SELECT posting,active_item FROM chats WHERE id=?", (chat_id,)).fetchone()
            self.assertEqual(tuple(old), ("LEGACY-POSTING", item))
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
            states = {row["error"]: row["state"] for row in con.execute(
                "SELECT error,state FROM jobs WHERE payload IN (?,?)",
                (db.dump({"posting": "NEXT-POSTING"}), db.dump({"posting": "UNCERTAIN-POSTING"})),
            )}
            self.assertEqual(states, {None: "pending", "transport_unknown": "unknown"})
            worker.import_chat(con, 1, {"chat_id": "SHARED"}, posting="NEXT-POSTING")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM chats WHERE external_id='SHARED'").fetchone()[0], 2)


class FolioIntegrationsTest(unittest.TestCase):
    setUp = FolioTest.setUp
    tearDown = FolioTest.tearDown
    setup_instance = FolioTest.setup_instance
    image = FolioTest.image
    image_worker_settings = FolioTest.image_worker_settings

    def test_image_worker_size_from_seller_article(self):
        self.assertEqual(image_tasks.dimensions_from_article("КАРТЕСТ-01_40х60"), (40, 60))
        self.assertEqual(image_tasks.dimensions_from_article("PORTRAIT_50x70"), (50, 70))
        self.assertEqual(image_tasks.dimensions_from_article("PORTRAIT_60×80"), (60, 80))
        self.assertEqual(image_tasks.dimensions_from_article("КАРТЕСТ-01"), (40, 60))
        with self.assertRaises(image_tasks.ImageTaskError):
            image_tasks.dimensions_from_article("КАРТЕСТ-01_500х600")
        with self.assertRaises(image_tasks.ImageTaskError):
            image_tasks.dimensions_from_article("КАРТЕСТ-01_40x")

    def test_scenario_image_job_waits_for_worker_and_queue_dispatches_one_at_a_time(self):
        config = {
            "FOLIO_IMAGE_WORKER_URL": "https://worker.example.com",
            "FOLIO_IMAGE_WORKER_API_KEY": "KEY",
            "FOLIO_IMAGE_WORKER_WEBHOOK_SECRET": "KEY",
            "FOLIO_IMAGE_WORKER_WEBHOOK_URL": "https://one.example.com/folio/image-worker/webhook",
        }
        graph_value = {"nodes": [
            {"id": "start", "kind": "start", "next": "photo"},
            {"id": "photo", "kind": "ask_photo", "field": "photos", "required": True,
             "text": "Пришлите фото", "min": 1, "max": 1, "next": "image"},
            {"id": "image", "kind": "image_worker", "prompt": "Фон: {background}",
             "next": "ready", "error": "handoff"},
            {"id": "handoff", "kind": "handoff", "next": "end"},
            {"id": "ready", "kind": "ready"},
            {"id": "end", "kind": "end"},
        ]}
        scenarios.validate(graph_value, "portrait_background")
        self.assertEqual(simulate(graph_value, "portrait_background", ["photo:1"])["instance"]["status"],
                         "needs_review")
        with self.image_worker_settings(config):
            instances = []
            for suffix in ("A", "B"):
                instance_id, item_id, chat_id, _ = self.setup_instance(
                    posting=f"ORDER-{suffix}", external_chat=f"CHAT-{suffix}"
                )
                with db.transaction() as con:
                    media_id = self.image(con, chat_id, item_id)
                    version_id = con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0]
                    con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump(graph_value), version_id))
                    con.execute("UPDATE items SET offer_id=? WHERE id=?", (f"КАРТЕСТ-{suffix}_50х70", item_id))
                    con.execute("UPDATE instances SET fields=?,node='image',status='collecting' WHERE id=?",
                                (db.dump({"photos": [media_id], "background": "светлый"}), instance_id))
                    scenarios.advance(con, instance_id)
                    scenarios.advance(con, instance_id)
                    row = con.execute("SELECT * FROM image_jobs WHERE instance_id=?", (instance_id,)).fetchone()
                    self.assertEqual((row["width_cm"], row["height_cm"]), (50, 70))
                    self.assertEqual(row["prompt"], "Фон: светлый")
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM image_jobs WHERE instance_id=?", (instance_id,)).fetchone()[0], 1)
                    self.assertEqual(con.execute("SELECT status FROM instances WHERE id=?", (instance_id,)).fetchone()[0], "waiting_integration")
                    instances.append((instance_id, row["id"]))
            worker_ids = ["924539a0-d73e-4d62-a16d-2d35ce91a2cf", "fa35425e-a7d4-4b4f-831a-8033d24a7eaf"]
            sends = []
            def handler(request):
                sends.append(request.url.path)
                return httpx.Response(202, json={"id": worker_ids[len(sends) - 1], "status": "queued"})
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                self.assertTrue(image_tasks.submit_one(client=client))
                self.assertFalse(image_tasks.submit_one(client=client))
                self.assertEqual(len(sends), 1)
                self.assertTrue(image_tasks.apply_result(worker_ids[0], {
                    "id": worker_ids[0], "status": "completed", "print_file": "\\\\server\\prints\\one.tif"
                }, "completed"))
                self.assertTrue(image_tasks.submit_one(client=client))
            self.assertEqual(len(sends), 2)
            with db.transaction() as con:
                first = con.execute("SELECT status FROM instances WHERE id=?", (instances[0][0],)).fetchone()[0]
                second = con.execute("SELECT status FROM instances WHERE id=?", (instances[1][0],)).fetchone()[0]
            self.assertEqual(first, "needs_review")
            self.assertEqual(second, "waiting_integration")

    def test_image_worker_step_sends_multiple_photos_to_manager_without_job(self):
        instance_id, item_id, chat_id, _ = self.setup_instance()
        graph_value = {"nodes": [
            {"id": "start", "kind": "start", "next": "photo"},
            {"id": "photo", "kind": "ask_photo", "field": "photos", "required": True,
             "text": "Пришлите фото", "min": 1, "max": 2, "accept": "готово", "next": "image"},
            {"id": "image", "kind": "image_worker", "prompt": "Обработать фото",
             "next": "ready", "error": "handoff"},
            {"id": "handoff", "kind": "handoff", "next": "end"},
            {"id": "ready", "kind": "ready"}, {"id": "end", "kind": "end"},
        ]}
        scenarios.validate(graph_value, "portrait_background")
        with db.transaction() as con:
            photos = [self.image(con, chat_id, item_id) for _ in range(2)]
            version = con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0]
            con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump(graph_value), version))
            con.execute("UPDATE instances SET fields=?,node='image',status='collecting' WHERE id=?",
                        (db.dump({"photos": photos}), instance_id))
            scenarios.advance(con, instance_id)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM image_jobs").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT status FROM instances WHERE id=?", (instance_id,)).fetchone()[0],
                             "needs_manager")
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat_id,)).fetchone()[0], "manual")

    def test_image_worker_failure_uses_error_branch_and_legacy_size(self):
        config = {
            "FOLIO_IMAGE_WORKER_URL": "https://worker.example.com",
            "FOLIO_IMAGE_WORKER_API_KEY": "KEY",
            "FOLIO_IMAGE_WORKER_WEBHOOK_SECRET": "KEY",
            "FOLIO_IMAGE_WORKER_WEBHOOK_URL": "https://one.example.com/folio/image-worker/webhook",
        }
        instance_id, item_id, chat_id, _ = self.setup_instance()
        graph_value = {"nodes": [
            {"id": "start", "kind": "start", "next": "photo"},
            {"id": "photo", "kind": "ask_photo", "field": "photos", "required": True,
             "text": "Пришлите фото", "min": 1, "max": 1, "next": "image"},
            {"id": "image", "kind": "image_worker", "prompt": "Обработать фото",
             "next": "ready", "error": "handoff"},
            {"id": "handoff", "kind": "handoff", "next": "end"},
            {"id": "ready", "kind": "ready"}, {"id": "end", "kind": "end"},
        ]}
        with self.image_worker_settings(config):
            with db.transaction() as con:
                photo = self.image(con, chat_id, item_id)
                version = con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0]
                con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump(graph_value), version))
                con.execute("UPDATE instances SET fields=?,node='image',status='collecting' WHERE id=?",
                            (db.dump({"photos": [photo]}), instance_id))
                scenarios.advance(con, instance_id)
                job = con.execute("SELECT * FROM image_jobs").fetchone()
                self.assertEqual((job["width_cm"], job["height_cm"]), (40, 60))
            worker_id = "924539a0-d73e-4d62-a16d-2d35ce91a2cf"
            with httpx.Client(transport=httpx.MockTransport(
                lambda _request: httpx.Response(202, json={"id": worker_id, "status": "queued"})
            )) as client:
                self.assertTrue(image_tasks.submit_one(client=client))
            image_tasks.apply_result(worker_id, {"id": worker_id, "status": "failed",
                                                 "error": "image_worker_media_missing"}, "failed")
            with db.transaction() as con:
                instance = con.execute("SELECT node,status,fields FROM instances WHERE id=?", (instance_id,)).fetchone()
                self.assertEqual((instance["node"], instance["status"]), ("handoff", "needs_manager"))
                self.assertIn("недоступно", json.loads(instance["fields"])["integration_error"])

    def test_scenario_import_export_creates_unpublished_draft_without_mapping(self):
        instance_id, _, _, scenario_id = self.setup_instance()
        exported = self.client.get(f"/scenarios/{scenario_id}/export?source=published", headers=self.admin)
        self.assertEqual(exported.status_code, 200)
        self.assertIn("attachment", exported.headers["content-disposition"])
        self.assertEqual(exported.json()["format"], scenarios.TRANSFER_FORMAT)
        self.assertEqual(self.client.get(f"/scenarios/{scenario_id}/export", headers=self.manager).status_code, 403)
        denied = self.client.post("/scenarios/import", headers=self.manager, data={"csrf": "csrf"},
                                  files={"file": ("scenario.json", exported.content, "application/json")})
        self.assertEqual(denied.status_code, 403)
        imported = self.client.post("/scenarios/import", headers=self.admin, data={"csrf": "csrf"},
                                    files={"file": ("scenario.json", exported.content, "application/json")},
                                    follow_redirects=False)
        self.assertEqual(imported.status_code, 303)
        with db.transaction() as con:
            new = con.execute("SELECT * FROM scenarios ORDER BY id DESC LIMIT 1").fetchone()
            self.assertNotEqual(new["id"], scenario_id)
            self.assertIsNone(new["published"])
            self.assertEqual(json.loads(new["draft"]), exported.json()["graph"])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM mappings WHERE scenario_id=?", (new["id"],)).fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT scenario_id FROM versions WHERE id=(SELECT version_id FROM instances WHERE id=?)", (instance_id,)).fetchone()[0], scenario_id)
        bad = self.client.post("/scenarios/import", headers=self.admin, data={"csrf": "csrf"},
                               files={"file": ("bad.json", b'{"format":"bad"}', "application/json")})
        self.assertEqual(bad.status_code, 422)

    def test_image_worker_environment_values_do_not_enable_integration(self):
        with patch.dict(os.environ, {
            "FOLIO_IMAGE_WORKER_URL": "https://ignored.example.com",
            "FOLIO_IMAGE_WORKER_API_KEY": "IGNORED",
            "FOLIO_IMAGE_WORKER_WEBHOOK_SECRET": "IGNORED",
            "FOLIO_IMAGE_WORKER_WEBHOOK_URL": "https://ignored.example.com/webhook",
        }):
            self.assertFalse(image_tasks.configured())
            self.assertIn("Воркер изображений не настроен",
                          self.client.get("/image-jobs", headers=self.admin).text)

    def test_image_worker_settings_are_persistent_masked_and_admin_only(self):
        values = {
            "FOLIO_IMAGE_WORKER_URL": "https://worker.example.com",
            "FOLIO_IMAGE_WORKER_API_KEY": "PRIVATE_WORKER_KEY",
            "FOLIO_IMAGE_WORKER_WEBHOOK_SECRET": "PRIVATE_WEBHOOK_SECRET",
            "FOLIO_IMAGE_WORKER_WEBHOOK_URL": "https://one.example.com/folio/image-worker/webhook",
        }
        denied = self.client.post("/settings/image-worker", headers=self.manager,
                                  data={"csrf": "csrf", "base_url": values["FOLIO_IMAGE_WORKER_URL"],
                                        "webhook_url": values["FOLIO_IMAGE_WORKER_WEBHOOK_URL"],
                                        "api_key": "SECRET", "webhook_secret": "SECRET"})
        self.assertEqual(denied.status_code, 403)
        with self.image_worker_settings(values):
            page = self.client.get("/settings/image-worker", headers=self.admin)
            self.assertEqual(page.status_code, 200)
            self.assertNotIn("PRIVATE_WORKER_KEY", page.text)
            self.assertNotIn("PRIVATE_WEBHOOK_SECRET", page.text)
            self.assertIn("FOLIO_POLL_URL=https://one.example.ru/folio/image-worker/next", page.text)
            self.assertEqual(self.client.get("/settings/image-worker", headers=self.manager).status_code, 403)
            with db.transaction() as con:
                row = image_tasks.integration(con)
                self.assertNotIn("PRIVATE_WORKER_KEY", row["api_key_secret"])
                self.assertNotIn("PRIVATE_WEBHOOK_SECRET", row["webhook_secret"])
            db.initialize()  # a container restart re-opens the same durable volume
            self.assertEqual(image_tasks.configuration()[1:3],
                             ("PRIVATE_WORKER_KEY", "PRIVATE_WORKER_KEY"))
            kept = self.client.post("/settings/image-worker", headers=self.admin,
                                    data={"csrf": "csrf", "base_url": values["FOLIO_IMAGE_WORKER_URL"],
                                          "webhook_url": values["FOLIO_IMAGE_WORKER_WEBHOOK_URL"],
                                          "api_key": "", "webhook_secret": ""}, follow_redirects=False)
            self.assertEqual(kept.status_code, 303)
            self.assertEqual(image_tasks.configuration()[1:3],
                             ("PRIVATE_WORKER_KEY", "PRIVATE_WORKER_KEY"))
            self.assertEqual(self.client.post("/settings/image-worker/probe", headers=self.manager,
                                              data={"csrf": "csrf"}).status_code, 403)
            with patch("app.main.image_tasks.probe", return_value=(True, None)):
                self.assertEqual(self.client.post("/settings/image-worker/probe", headers=self.admin,
                                                  data={"csrf": "csrf"}, follow_redirects=False).status_code, 303)
            self.assertIn("Ожидаем первый запрос воркера", self.client.get("/settings/image-worker", headers=self.admin).text)

    def test_image_worker_probe_checks_recent_pull(self):
        values = {
            "FOLIO_IMAGE_WORKER_URL": "https://worker.example.com",
            "FOLIO_IMAGE_WORKER_API_KEY": "PROBE_KEY",
            "FOLIO_IMAGE_WORKER_WEBHOOK_SECRET": "PROBE_SECRET",
            "FOLIO_IMAGE_WORKER_WEBHOOK_URL": "https://one.example.com/folio/image-worker/webhook",
        }
        with self.image_worker_settings(values):
            self.assertEqual(image_tasks.probe(), (False, "image_worker_no_recent_poll"))
            response = self.client.get("/image-worker/next", headers={"Authorization": "Bearer PROBE_KEY"})
            self.assertEqual(response.status_code, 204)
            self.assertEqual(image_tasks.probe(), (True, None))
    def test_approval_dictionary_uses_exact_safe_phrases(self):
        node = {"dictionary_version": 1, "accept": "СЛУЧАЙНАЯ ФРАЗА", "reject": "СЛУЧАЙНЫЙ ОТКАЗ"}
        self.assertIn(scenarios.approval_phrase("Да!"), scenarios.approval_variants(node, "accept"))
        self.assertIn(scenarios.approval_phrase("НЕ ПОДТВЕРЖДАЮ."), scenarios.approval_variants(node, "reject"))
        self.assertNotIn(scenarios.approval_phrase("Да, но переделайте фон"), scenarios.approval_variants(node, "accept"))
        self.assertNotIn(scenarios.approval_phrase("СЛУЧАЙНАЯ ФРАЗА"), scenarios.approval_variants(node, "accept"))
        self.assertNotIn(scenarios.approval_phrase("СЛУЧАЙНЫЙ ОТКАЗ"), scenarios.approval_variants(node, "reject"))
        self.assertEqual(scenarios.approval_variants({"accept": "ДА"}, "accept"), {"да"})
        self.assertIn("принято", scenarios.approval_variants({"use_dictionary": True, "accept": "Принято"}, "accept"))

    def test_approval_draft_needs_no_manual_phrases(self):
        value = {"nodes": [{"id": "approval", "kind": "approval", "accept": "ДА", "reject": "НЕТ", "hours": 6}]}
        self.assertTrue(scenarios.normalize(value, "portrait_background"))
        node = value["nodes"][0]
        self.assertEqual(node["dictionary_version"], 1)
        self.assertNotIn("accept", node)
        self.assertNotIn("reject", node)
        self.assertNotIn("use_dictionary", node)
        _instance, _item, _chat, scenario_id = self.setup_instance()
        page = self.client.get(f"/scenarios/{scenario_id}", headers=self.admin)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Дополнительные варианты подтверждения", page.text)
        self.assertNotIn("Дополнительные варианты отказа", page.text)

    def test_mattermost_settings_are_admin_only_and_secret_is_masked(self):
        url = "https://chat.example.com/hooks/private-webhook-token"
        denied = self.client.post("/settings/mattermost", headers=self.manager,
                                  data={"csrf": "csrf", "webhook_url": url})
        self.assertEqual(denied.status_code, 403)
        saved = self.client.post("/settings/mattermost", headers=self.admin,
                                 data={"csrf": "csrf", "webhook_url": url}, follow_redirects=False)
        self.assertEqual(saved.status_code, 303)
        page = self.client.get("/settings/mattermost", headers=self.admin)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("private-webhook-token", page.text)
        with db.transaction() as con:
            record = mattermost.integration(con)
            self.assertNotIn("private-webhook-token", record["secret"])
            self.assertEqual(mattermost.webhook_url(record), url)

    def test_mattermost_node_queues_then_advances_after_send(self):
        instance_id, _item_id, _chat_id, _ = self.setup_instance()
        value = {"nodes": [
            {"id": "start", "kind": "start", "next": "notify"},
            {"id": "notify", "kind": "mattermost", "message": "Заказ {posting}: ошибка {integration_error}",
             "next": "end", "error": "end"},
            {"id": "end", "kind": "end"},
        ]}
        with db.transaction() as con:
            version = con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0]
            con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump(value), version))
            con.execute("UPDATE instances SET node='start',fields=? WHERE id=?",
                        (db.dump({"integration_error": "Нет фотографии"}), instance_id))
            con.execute("INSERT INTO mattermost_integrations(id,secret) VALUES(1,?)",
                        (security.cipher().encrypt(b"https://chat.example.com/hooks/private").decode(),))
            scenarios.advance(con, instance_id)
            job = con.execute("SELECT * FROM jobs WHERE kind='mattermost_send'").fetchone()
            self.assertIsNotNone(job)
            self.assertEqual(con.execute("SELECT status FROM instances WHERE id=?", (instance_id,)).fetchone()[0], "waiting_integration")
        sent = []
        with patch("app.worker.mattermost.send", side_effect=lambda record, message: sent.append(message)):
            self.assertTrue(worker.tick())
        self.assertEqual(sent, ["Заказ ORDER: ошибка Нет фотографии"])
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT status FROM instances WHERE id=?", (instance_id,)).fetchone()[0], "closed")
            self.assertEqual(con.execute("SELECT state FROM mattermost_actions").fetchone()[0], "sent")
            self.assertEqual(con.execute("SELECT state FROM jobs WHERE id=?", (job["id"],)).fetchone()[0], "done")

    def test_mattermost_transport_unknown_does_not_retry(self):
        record = {"secret": security.cipher().encrypt(b"https://chat.example.com/hooks/private").decode()}
        def timeout(_request):
            raise httpx.ReadTimeout("timeout")
        with self.assertRaises(mattermost.MattermostError) as raised:
            mattermost.send(record, "Ошибка", transport=httpx.MockTransport(timeout))
        self.assertTrue(raised.exception.unknown)

    def test_mattermost_webhook_posts_text_and_rejects_bad_url(self):
        with self.assertRaises(ValueError):
            mattermost.normalize_webhook_url("http://127.0.0.1/hooks/private")
        record = {"secret": security.cipher().encrypt(b"https://chat.example.com/hooks/private").decode()}
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, text="ok")
        mattermost.send(record, "Ошибка заказа", transport=httpx.MockTransport(handler))
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(json.loads(requests[0].content), {"text": "Ошибка заказа"})

    def test_retailcrm_status_reference_filters_inactive(self):
        with db.transaction() as con:
            con.execute("INSERT INTO retailcrm_integrations(id,base_url,site,secret) VALUES(1,?,?,?)",
                        ("https://shop.retailcrm.ru", "shop", security.cipher().encrypt(b"KEY").decode()))
            record = retailcrm.integration(con)
        def handler(request):
            self.assertEqual(request.url.path, "/api/v5/reference/statuses")
            return httpx.Response(200, json={"success": True, "statuses": [
                {"code": "approved", "name": "Одобрено", "group": "complete", "active": True},
                {"code": "old", "name": "Старый", "active": False},
            ]})
        adapter = retailcrm.RetailCRMAdapter(record, transport=httpx.MockTransport(handler))
        try:
            self.assertEqual(adapter.statuses(), [{"code": "approved", "name": "Одобрено", "group": "complete"}])
        finally:
            adapter.close()

    def test_retailcrm_status_reference_accepts_code_keyed_object(self):
        with db.transaction() as con:
            con.execute("INSERT INTO retailcrm_integrations(id,base_url,site,secret) VALUES(1,?,?,?)",
                        ("https://shop.retailcrm.ru", "shop", security.cipher().encrypt(b"KEY").decode()))
            record = retailcrm.integration(con)
        def handler(_request):
            return httpx.Response(200, json={"success": True, "statuses": {
                "approved": {"code": "approved", "name": "Одобрено", "group": "complete", "active": True},
                "old": {"code": "old", "name": "Старый", "active": False},
            }})
        adapter = retailcrm.RetailCRMAdapter(record, transport=httpx.MockTransport(handler))
        try:
            self.assertEqual(adapter.statuses(), [{"code": "approved", "name": "Одобрено", "group": "complete"}])
        finally:
            adapter.close()

    def test_scenario_editor_offers_cached_retailcrm_statuses(self):
        with db.transaction() as con:
            scenario_id = con.execute(
                "INSERT INTO scenarios(name,product_type,draft) VALUES(?,?,?)",
                ("Проверка", "portrait_background", db.dump(graph("portrait_background"))),
            ).lastrowid
            con.execute(
                "INSERT INTO retailcrm_integrations(id,base_url,site,secret,statuses) VALUES(1,?,?,?,?)",
                ("https://shop.retailcrm.ru", "shop", security.cipher().encrypt(b"KEY").decode(),
                 db.dump([{"code": "approved", "name": "Одобрено"}])),
            )
        response = self.client.get(f"/scenarios/{scenario_id}", headers=self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="approved"', response.text)
        self.assertIn("Одобрено (approved)", response.text)

    def test_retailcrm_status_edit_payload_keeps_items(self):
        from urllib.parse import parse_qs
        with db.transaction() as con:
            con.execute("INSERT INTO retailcrm_integrations(id,base_url,site,secret) VALUES(1,?,?,?)",
                        ("https://shop.retailcrm.ru", "shop", security.cipher().encrypt(b"KEY").decode()))
            record = retailcrm.integration(con)
        orders = []
        def handler(request):
            orders.append(json.loads(parse_qs(request.content.decode())["order"][0]))
            return httpx.Response(200, json={"success": True})
        adapter = retailcrm.RetailCRMAdapter(record, transport=httpx.MockTransport(handler))
        try:
            adapter.edit_order_customer_comment("folio_instance_7", "Старый\n\nИтог", [{"id": 11}, {"id": 12}], "approved")
        finally:
            adapter.close()
        self.assertEqual(orders, [{"customerComment": "Старый\n\nИтог", "status": "approved",
                                   "items": [{"id": 11}, {"id": 12}]}])

    def test_retailcrm_note_changes_only_selected_status_and_keeps_items(self):
        instance_id, _item_id, _chat_id, _ = self.setup_instance()
        value = {"nodes": [
            {"id": "start", "kind": "start", "next": "note"},
            {"id": "note", "kind": "retailcrm_note", "comment": "Итог", "status": "approved",
             "next": "end", "error": "end"},
            {"id": "end", "kind": "end"},
        ]}
        with db.transaction() as con:
            version = con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0]
            con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump(value), version))
            con.execute("UPDATE instances SET node='note',status='waiting_integration' WHERE id=?", (instance_id,))
            con.execute("INSERT INTO retailcrm_integrations(id,base_url,site,secret,capabilities,checked_at,statuses) VALUES(1,?,?,?,?,?,?)",
                        ("https://shop.retailcrm.ru", "shop", security.cipher().encrypt(b"KEY").decode(),
                         db.dump({"order_read": True, "order_write": True, "site": True}), db.now(),
                         db.dump([{"code": "approved", "name": "Одобрено"}])))
            action_id = con.execute("INSERT INTO retailcrm_actions(instance_id,node_id,external_id,created_at,kind) VALUES(?,?,?,?,?)",
                                    (instance_id, "note", retailcrm.external_id(instance_id), db.now(), "note")).lastrowid
            db.job(con, "retailcrm_note", 1, {"action_id": action_id})
            job = con.execute("SELECT * FROM jobs WHERE kind='retailcrm_note' ORDER BY id DESC LIMIT 1").fetchone()
        edited = []
        class FakeRetail:
            def __init__(self, _record): pass
            def find_order(self, _external_id):
                return {"order": {"id": 7, "status": "new", "customerComment": "Старый", "items": [{"id": 11}]}}
            def edit_order_customer_comment(self, *args): edited.append(args)
            def close(self): pass
        self.assertEqual(worker.retailcrm_note_job(job, FakeRetail), ("sent", None))
        self.assertEqual(edited[0][2], [{"id": 11}])
        self.assertEqual(edited[0][3:], ("approved",))

    def test_image_job_worker_pulls_and_uploads_preview(self):
        instance_id, item_id, chat_id, _ = self.setup_instance()
        config = {
            "FOLIO_IMAGE_WORKER_URL": "https://worker.example.com",
            "FOLIO_IMAGE_WORKER_API_KEY": "TEST_WORKER_KEY",
            "FOLIO_IMAGE_WORKER_WEBHOOK_SECRET": "TEST_SIGNING_SECRET",
            "FOLIO_IMAGE_WORKER_WEBHOOK_URL": "https://one.example.com/folio/image-worker/webhook",
        }
        with self.image_worker_settings(config):
            with db.transaction() as con:
                mids = [self.image(con, chat_id, item_id) for _ in range(8)]
                con.execute("UPDATE instances SET fields=? WHERE id=?",
                            (db.dump({"photos": mids}), instance_id))
            self.assertEqual(self.client.get("/image-jobs", headers=self.manager).status_code, 403)
            listed = self.client.get("/image-jobs", headers=self.admin)
            self.assertEqual(listed.status_code, 200)
            with db.transaction() as con:
                image_tasks.enqueue(con, item_id, mids, "Новый фон", 40, 60, "1")
                job = con.execute("SELECT * FROM image_jobs").fetchone()
                self.assertEqual(job["state"], "pending")
                self.assertEqual(job["item_id"], item_id)
            self.assertIn("ORDER", self.client.get("/image-jobs", headers=self.admin).text)
            self.assertEqual(self.client.get("/image-worker/next").status_code, 401)
            auth = {"Authorization": "Bearer TEST_WORKER_KEY"}
            response = self.client.get("/image-worker/next", headers=auth)
            self.assertEqual(response.status_code, 200, response.text)
            pulled = response.json()
            self.assertEqual(pulled["id"], job["id"])
            self.assertEqual(len(pulled["image_urls"]), 8)
            self.assertEqual(self.client.get("/image-worker/next", headers=auth).json(), pulled)
            for url in pulled["image_urls"]:
                self.assertEqual(self.client.get(url.replace("https://one.example.ru/folio", "")).status_code, 200)
            with db.transaction() as con:
                self.assertEqual(con.execute("SELECT state FROM image_jobs").fetchone()["state"], "queued")
            endpoint = f"/image-worker/jobs/{job['id']}"
            self.assertEqual(self.client.post(endpoint, data={"worker_id": pulled["worker_id"], "status": "generating"}).status_code, 401)
            self.assertEqual(self.client.post(endpoint, headers=auth, data={"worker_id": pulled["worker_id"], "status": "generating"}).status_code, 200)
            preview = io.BytesIO()
            Image.new("RGB", (667, 1000), "blue").save(preview, format="JPEG")
            data = {"worker_id": pulled["worker_id"], "status": "completed", "print_file": r"\\server\prints\file.tif"}
            files = {"preview": ("preview.jpg", preview.getvalue(), "image/jpeg")}
            accepted = self.client.post(endpoint, headers=auth, data=data, files=files)
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(self.client.post(endpoint, headers=auth, data=data, files=files).status_code, 200)
            with db.transaction() as con:
                result = con.execute("SELECT state,print_file,preview_media_id FROM image_jobs").fetchone()
                self.assertEqual(result["state"], "completed")
                self.assertEqual(result["print_file"], data["print_file"])
                self.assertIsNotNone(result["preview_media_id"])
                self.assertEqual(con.execute("SELECT COUNT(*) FROM media WHERE id=?", (result["preview_media_id"],)).fetchone()[0], 1)
            self.assertEqual(self.client.get(f"/image-jobs/{job['id']}/preview", headers=self.manager).status_code, 403)
            response = self.client.get(f"/image-jobs/{job['id']}/preview", headers=self.admin)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, preview.getvalue())
            self.assertEqual(self.client.get("/image-worker/next", headers=auth).status_code, 204)

    def test_send_mockup_block_sends_worker_preview(self):
        instance_id, item_id, chat_id, _ = self.setup_instance()
        with self.image_worker_settings({
            "FOLIO_IMAGE_WORKER_URL": "https://unused.example.com",
            "FOLIO_IMAGE_WORKER_API_KEY": "MOCKUP_KEY",
        }):
            with db.transaction() as con:
                mid = self.image(con, chat_id, item_id)
                version = con.execute("SELECT version_id FROM instances WHERE id=?", (instance_id,)).fetchone()[0]
                value = json.loads(con.execute("SELECT graph FROM versions WHERE id=?", (version,)).fetchone()[0])
                value["nodes"].extend([
                    {"id": "make", "kind": "image_worker", "prompt": "Новый фон", "next": "mockup", "error": "ready"},
                    {"id": "mockup", "kind": "send_mockup", "next": "ready"},
                ])
                con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump(value), version))
                con.execute("UPDATE instances SET node='make',status='waiting_integration',fields=? WHERE id=?",
                            (db.dump({"photos": [mid], "background": "Синий"}), instance_id))
                image_tasks.enqueue(con, item_id, mid, "Новый фон", 40, 60, "1",
                                    instance_id=instance_id, node_id="make")
            auth = {"Authorization": "Bearer MOCKUP_KEY"}
            pulled = self.client.get("/image-worker/next", headers=auth).json()
            preview = io.BytesIO()
            Image.new("RGB", (667, 1000), "blue").save(preview, format="JPEG")
            response = self.client.post(f"/image-worker/jobs/{pulled['id']}", headers=auth,
                data={"worker_id": pulled["worker_id"], "status": "completed", "print_file": "local.tif"},
                files={"preview": ("preview.jpg", preview.getvalue(), "image/jpeg")})
            self.assertEqual(response.status_code, 200, response.text)
            with db.transaction() as con:
                instance = con.execute("SELECT node,status FROM instances WHERE id=?", (instance_id,)).fetchone()
                self.assertEqual((instance["node"], instance["status"]), ("mockup", "waiting_integration"))
                outgoing = con.execute("SELECT * FROM outbox WHERE instance_id=? AND kind='file'", (instance_id,)).fetchone()
                self.assertIsNotNone(outgoing)
                self.assertEqual(outgoing["media_id"], con.execute("SELECT preview_media_id FROM image_jobs").fetchone()[0])
                con.execute("UPDATE outbox SET state='sent' WHERE id=?", (outgoing["id"],))
                scenarios.outgoing_confirmed(con, outgoing["id"])
                self.assertEqual(con.execute("SELECT node FROM instances WHERE id=?", (instance_id,)).fetchone()[0], "ready")

    def test_image_job_uncertain_submit_is_never_repeated(self):
        instance_id, item_id, chat_id, _ = self.setup_instance()
        with self.image_worker_settings({
            "FOLIO_IMAGE_WORKER_URL": "https://worker.example.com",
            "FOLIO_IMAGE_WORKER_API_KEY": "KEY",
            "FOLIO_IMAGE_WORKER_WEBHOOK_SECRET": "SECRET",
            "FOLIO_IMAGE_WORKER_WEBHOOK_URL": "https://one.example.com/folio/image-worker/webhook",
        }):
            with db.transaction() as con:
                mid = self.image(con, chat_id, item_id)
                con.execute("UPDATE instances SET fields=? WHERE id=?", (db.dump({"photos": [mid]}), instance_id))
                image_tasks.enqueue(con, item_id, mid, "Новый фон", 40, 60, "1")
            calls = []
            def timeout(request):
                calls.append(request)
                raise httpx.ReadTimeout("Lost response")
            with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
                self.assertTrue(image_tasks.submit_one(client=client))
                self.assertFalse(image_tasks.submit_one(client=client))
            self.assertEqual(len(calls), 1)
            with db.transaction() as con:
                result = con.execute("SELECT state,worker_id FROM image_jobs").fetchone()
                self.assertEqual(result["state"], "unknown")
                self.assertIsNone(result["worker_id"])

    def test_image_job_sends_eight_images_in_order(self):
        instance_id, item_id, chat_id, _ = self.setup_instance()
        with self.image_worker_settings({
            "FOLIO_IMAGE_WORKER_URL": "http://host.docker.internal:8000",
            "FOLIO_IMAGE_WORKER_API_KEY": "KEY",
            "FOLIO_IMAGE_WORKER_WEBHOOK_SECRET": "SECRET",
            "FOLIO_IMAGE_WORKER_WEBHOOK_URL": "https://one.example.com/folio/image-worker/webhook",
            "FOLIO_IMAGE_WORKER_ALLOW_HTTP": "true",
            "FOLIO_IMAGE_WORKER_INPUT_BASE_URL": "https://one.example.com",
        }):
            with db.transaction() as con:
                media_ids = [self.image(con, chat_id, item_id) for _ in range(8)]
                con.execute("UPDATE instances SET fields=? WHERE id=?",
                            (db.dump({"photos": media_ids}), instance_id))
                task_id = image_tasks.enqueue(con, item_id, media_ids, "Новый фон", 40, 60, "1")
                token = con.execute("SELECT input_token FROM image_jobs WHERE id=?", (task_id,)).fetchone()[0]
                with self.assertRaises(image_tasks.ImageTaskError):
                    image_tasks.enqueue(con, item_id, media_ids + [media_ids[0]], "Новый фон", 40, 60, "1")
            for index in range(8):
                self.assertEqual(self.client.get(f"/image-worker/input/{token}/{index}").status_code, 200)
            self.assertEqual(self.client.get(f"/image-worker/input/{token}/8").status_code, 404)
            def submit(request):
                from urllib.parse import parse_qs
                self.assertEqual(parse_qs(request.content.decode())["image_url"], [
                    f"https://one.example.ru/folio/image-worker/input/{token}/{index}" for index in range(8)
                ])
                return httpx.Response(202, json={"id": "924539a0-d73e-4d62-a16d-2d35ce91a2cf", "status": "queued"})
            with httpx.Client(transport=httpx.MockTransport(submit)) as client:
                self.assertTrue(image_tasks.submit_one(client=client))

    def test_image_job_local_worker_uses_tokenized_https_input_and_poll(self):
        instance_id, item_id, chat_id, _ = self.setup_instance()
        worker_id = "924539a0-d73e-4d62-a16d-2d35ce91a2cf"
        with self.image_worker_settings({
            "FOLIO_IMAGE_WORKER_URL": "http://host.docker.internal:8000",
            "FOLIO_IMAGE_WORKER_API_KEY": "KEY",
            "FOLIO_IMAGE_WORKER_WEBHOOK_SECRET": "SECRET",
            "FOLIO_IMAGE_WORKER_WEBHOOK_URL": "https://one.example.com/folio/image-worker/webhook",
            "FOLIO_IMAGE_WORKER_ALLOW_HTTP": "true",
            "FOLIO_IMAGE_WORKER_INPUT_BASE_URL": "https://one.example.com",
            "FOLIO_IMAGE_WORKER_VENDOR_TOKEN": "A" * 40,
        }):
            with db.transaction() as con:
                mid = self.image(con, chat_id, item_id)
                con.execute("UPDATE instances SET fields=? WHERE id=?", (db.dump({"photos": [mid]}), instance_id))
                task_id = image_tasks.enqueue(con, item_id, mid, "Новый фон", 40, 60, "1")
                token = con.execute("SELECT input_token FROM image_jobs WHERE id=?", (task_id,)).fetchone()[0]
            source = self.client.get(f"/image-worker/input/{token}")
            self.assertEqual(source.status_code, 200)
            self.assertEqual(source.headers["content-type"], "image/png")
            self.assertEqual(self.client.get("/image-worker/input/wrong").status_code, 404)
            self.assertEqual(self.client.post("/image-worker/vendor-callback/wrong").status_code, 404)
            self.assertEqual(self.client.post("/image-worker/vendor-callback/" + image_tasks.vendor_token(),
                                              content=b'{}').status_code, 204)
            def submit(request):
                from urllib.parse import parse_qs
                self.assertNotIn(b'name="image"', request.content)
                self.assertEqual(parse_qs(request.content.decode())["image_url"],
                                 [f"https://one.example.ru/folio/image-worker/input/{token}/0"])
                return httpx.Response(202, json={"id": worker_id, "status": "queued"})
            with httpx.Client(transport=httpx.MockTransport(submit)) as client:
                self.assertTrue(image_tasks.submit_one(client=client))
            with db.transaction() as con:
                con.execute("UPDATE image_jobs SET next_poll_at=? WHERE id=?", (db.now(), task_id))
            def poll(request):
                self.assertEqual(request.url.path, f"/v1/jobs/{worker_id}")
                return httpx.Response(200, json={"id": worker_id, "status": "completed",
                                                 "print_file": "\\\\server\\prints\\result.tif"})
            with httpx.Client(transport=httpx.MockTransport(poll)) as client:
                self.assertTrue(image_tasks.poll_one(client=client))
            with db.transaction() as con:
                self.assertEqual(con.execute("SELECT state FROM image_jobs WHERE id=?", (task_id,)).fetchone()[0], "completed")
            self.assertEqual(self.client.get(f"/image-worker/input/{token}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
