import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from app import db, media, retailcrm, scenarios, security, worker
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
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = db.DATA
        db.DATA = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"FOLIO_ADMIN_USER_ID": "1"})
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
            con.execute("INSERT INTO users VALUES('2','Manager','manager')")
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
                (sku, kind, sid),
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
            chat = worker.import_chat(con, 1, {"chat_id": external_chat})
            con.execute("INSERT INTO chat_items VALUES(?,?)", (chat["id"], item))
            con.execute(
                "UPDATE chats SET mode='bot',active_item=? WHERE id=?",
                (item, chat["id"]),
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
                "needs_manager",
            )
            self.assertEqual(
                con.execute("SELECT mode FROM chats").fetchone()[0], "manual"
            )

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
            "/links",
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

    def test_adapter_uses_current_v4_fbs_posting_contract(self):
        with db.transaction() as con:
            account = dict(con.execute("SELECT * FROM accounts").fetchone())
        paths = []

        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(
                200,
                json={
                    "postings": [
                        {
                            "posting_number": "FBS-1",
                            "status": "awaiting_packaging",
                            "products": [],
                        }
                    ],
                    "has_next": False,
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

    def test_sku_not_offer_id_selects_scenario(self):
        with db.transaction() as con:
            scenario_ids = []
            for sku, kind in (("SKU-A", "portrait_background"), ("SKU-B", "collage")):
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
                        {"sku": "SKU-A", "offer_id": "SAME", "quantity": 1},
                        {"sku": "SKU-B", "offer_id": "SAME", "quantity": 1},
                    ],
                },
            )
            rows = con.execute(
                "SELECT i.sku,m.product_type FROM items i JOIN mappings m ON m.id=i.mapping_id ORDER BY i.sku"
            ).fetchall()
        self.assertEqual(
            [(row["sku"], row["product_type"]) for row in rows],
            [("SKU-A", "portrait_background"), ("SKU-B", "collage")],
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
                self.assertEqual(mapping["sku"], "SKU-1")
                self.assertEqual(item["mapping_id"], 1)
            finally:
                db.DATA = previous

    def test_catalog_and_builder_are_contextual(self):
        self.setup_instance()
        catalog = self.client.get("/catalog", headers=self.admin)
        self.assertIn("SKU и сценарии", catalog.text)
        self.assertIn('name="sku"', catalog.text)
        self.assertNotIn("Привязка offer_id", catalog.text)
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
                "automation_enabled": "on",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("подтвердите кабинет", response.text)

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
        self.assertEqual(response.status_code, 403)

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
                "VALUES(1,'retail-sku','portrait_background',?)",
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
                {"order_read": True, "order_write": True, "site": True},
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


if __name__ == "__main__":
    unittest.main()
