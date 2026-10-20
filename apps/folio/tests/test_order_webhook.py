import json
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path

import httpx

from app import db, order_webhook, scenarios
from tests import test_folio


class OrderWebhookTest(unittest.TestCase):
    setUp = test_folio.FolioTest.setUp
    tearDown = test_folio.FolioTest.tearDown
    setup_instance = test_folio.FolioTest.setup_instance
    image = test_folio.FolioTest.image

    def webhook_settings(self):
        return {"csrf": "csrf", "url": "https://mes.example.com",
                "send_token": "s" * 32, "shop_1": "12",
                "production_root": str(db.DATA / "production"),
                "worker_root": "D:/Print", "mount_root": str(db.DATA / "mount")}

    def fixture(self):
        iid, item, chat, sid = self.setup_instance()
        root = db.DATA / "mount"
        root.mkdir()
        (db.DATA / "production").mkdir()
        (root / "print.png").write_bytes(b"immutable-print")
        with db.transaction() as con:
            order_webhook.save(con, self.webhook_settings())
            con.execute("UPDATE items SET order_number='123-1' WHERE id=?", (item,))
            mid = self.image(con, chat, item)
            job = con.execute("INSERT INTO image_jobs(item_id,media_id,prompt,width_cm,height_cm,state,print_file,preview_media_id,created_at,updated_at,instance_id) VALUES(?,?,'prompt',40,60,'completed','D:/Print/print.png',?,?,?,?)",
                              (item, mid, mid, db.now(), db.now(), iid)).lastrowid
            con.execute("INSERT INTO outbox(chat_id,instance_id,actor,body,dedup,epoch,state,created_at,kind,media_id) VALUES(?,?,'bot','','sent-preview',0,'sent',?,'file',?)", (chat, iid, db.now(), mid))
            g = {"nodes": [{"id": "hook", "kind": "order_webhook", "next": "end", "error": "end"}, {"id": "end", "kind": "end"}]}
            con.execute("UPDATE versions SET graph=? WHERE id=(SELECT version_id FROM instances WHERE id=?)", (db.dump(g), iid))
            con.execute("UPDATE instances SET node='hook',prompted=0 WHERE id=?", (iid,))
            scenarios.advance(con, iid)
            action = con.execute("SELECT * FROM order_webhook_actions").fetchone()
            self.assertEqual(action["image_job_id"], job)
        return iid, action["id"]

    def test_delivery_snapshot_retry_download(self):
        iid, event = self.fixture()
        calls = []
        def sender(url, token, payload, key):
            calls.append((payload, key))
            return httpx.Response(503 if len(calls) == 1 else 202, json={"accepted": True, "external_order_id": "MES-1"})
        order_webhook.tick(sender)
        with db.transaction() as con:
            row = con.execute("SELECT * FROM order_webhook_actions").fetchone()
            self.assertEqual(row["state"], "pending")
            con.execute("UPDATE order_webhook_actions SET due_at='2000-01-01'")
        (db.DATA / "mount/print.png").write_bytes(b"changed")
        order_webhook.tick(sender)
        self.assertEqual(calls[0], calls[1])
        payload = json.loads(calls[0][0])
        self.assertNotIn("type", payload["order"])
        self.assertNotIn("seller_account_id", payload["order"])
        self.assertEqual(payload["order"]["shop_id"], 12)
        self.assertEqual(set(payload["print_file"]), {"id", "filename"})
        self.assertEqual(payload["order"]["width_cm"], 40)
        self.assertNotIn("approval_outcome", payload["order"])
        self.assertEqual(self.client.get(f"/integration-files/{event}").status_code, 410)
        self.assertEqual((db.DATA / "production" / event / "print.png").read_bytes(), b"immutable-print")
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT state FROM order_webhook_actions").fetchone()[0], "sent")
            self.assertEqual(con.execute("SELECT node FROM instances WHERE id=?", (iid,)).fetchone()[0], "end")
        self.assertFalse(order_webhook.tick(sender))

    def test_settings_masked_and_rbac(self):
        response = self.client.post("/settings/order-webhook", headers=self.admin, data=self.webhook_settings(), follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        page = self.client.get("/settings/order-webhook", headers=self.admin)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("s" * 32, page.text)
        self.assertNotIn("d" * 32, page.text)
        self.assertEqual(self.client.post("/settings/order-webhook", headers=self.manager, data=self.webhook_settings()).status_code, 403)
        self.assertEqual(self.client.get("/settings/order-webhook", headers=self.manager).status_code, 403)
        with db.transaction() as con:
            self.assertNotIn("s" * 32, con.execute("SELECT value FROM order_webhook_settings").fetchone()[0])

    def test_missing_file_and_manual_retry(self):
        iid, event = self.fixture()
        (db.DATA / "mount/print.png").unlink()
        order_webhook.tick(lambda *a: self.fail("No send without file"))
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT state FROM order_webhook_actions").fetchone()[0], "failed")
        (db.DATA / "mount/print.png").write_bytes(b"fixed")
        response = self.client.post(f"/settings/order-webhook/{event}/retry", headers=self.admin, data={"csrf": "csrf"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        order_webhook.tick(lambda *a: httpx.Response(202, json={"accepted": True, "external_order_id": "MES-1"}))
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT state FROM order_webhook_actions").fetchone()[0], "sent")

    def test_path_escape_and_url(self):
        root = db.DATA / "mount"
        root.mkdir()
        (db.DATA / "secret.png").write_bytes(b"secret")
        for name in ["D:/Print/../secret.png", "D:/Other/secret.png", "D:/Print/C:/secret.png"]:
            with self.assertRaises(ValueError):
                order_webhook.mounted_file(self.webhook_settings(), name)
        for url in ["http://example.com", "https://127.0.0.1", "https://example.com/?token=secret"]:
            with self.assertRaises(ValueError):
                order_webhook.https_url(url)

    def test_worker_unc_jpg_is_saved_before_mes_metadata(self):
        _, event = self.fixture()
        source = db.DATA / "mount" / "portrait.jpg"
        source.write_bytes(b"print-jpg")
        with db.transaction() as con:
            settings = self.webhook_settings()
            settings["worker_root"] = r"\\worker-pc\Print"
            order_webhook.save(con, settings)
            con.execute("UPDATE image_jobs SET print_file=?", (r"\\worker-pc\Print\portrait.jpg",))
        calls = []
        def sender(url, token, payload, key):
            self.assertEqual((db.DATA / "production" / event / "portrait.jpg").read_bytes(), b"print-jpg")
            calls.append(json.loads(payload))
            return httpx.Response(202, json={"accepted": True, "external_order_id": "MES-JPG"})
        self.assertTrue(order_webhook.tick(sender))
        self.assertEqual(calls[0]["print_file"]["filename"], "portrait.jpg")
        self.assertEqual(set(calls[0]["print_file"]), {"id", "filename"})

    def test_http_auth_redirect_not_followed(self):
        def handler(request):
            self.assertEqual(request.headers["Idempotency-Key"], "event")
            self.assertEqual(request.headers["Authorization"], "Bearer secret")
            self.assertEqual(request.headers["Accept"], "application/json")
            return httpx.Response(302, headers={"Location": "https://other.example"})
        result = order_webhook.send("https://laravel.example/hook", "secret", "{}", "event", transport=httpx.MockTransport(handler))
        self.assertEqual(result.status_code, 302)

    def test_dry_run_and_required_error_branch(self):
        iid, event = self.fixture()
        with db.transaction() as con:
            con.execute("DELETE FROM order_webhook_actions")
            scenarios.advance(con, iid, simulate_external=True)
            self.assertEqual(con.execute("SELECT count(*) FROM order_webhook_actions").fetchone()[0], 0)
        g = test_folio.graph("portrait_background")
        g["nodes"].insert(1, {"id": "webhook", "kind": "order_webhook", "title": "Webhook", "next": g["nodes"][0]["next"], "error": ""})
        g["nodes"][0]["next"] = "webhook"
        with self.assertRaises(scenarios.Invalid):
            scenarios.validate(g, "portrait_background")

    def test_manual_pause_and_cancel(self):
        iid, event = self.fixture()
        with db.transaction() as con:
            con.execute("UPDATE chats SET mode='manual'")
        self.assertFalse(order_webhook.tick(lambda *a: self.fail("manual")))
        with db.transaction() as con:
            con.execute("UPDATE order_webhook_actions SET due_at='2000-01-01'")
            con.execute("UPDATE items SET external_status='cancelled'")
        order_webhook.tick(lambda *a: self.fail("cancelled"))
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT state FROM order_webhook_actions").fetchone()[0], "failed")

    def test_crashed_send_reuses_snapshot(self):
        iid, event = self.fixture()
        order_webhook.tick(lambda *a: httpx.Response(503))
        with db.transaction() as con:
            snapshot = con.execute("SELECT payload FROM order_webhook_actions").fetchone()[0]
            con.execute("UPDATE order_webhook_actions SET state='sending',due_at='2000-01-01'")
        def sender(url, token, body, key):
            self.assertEqual(body, snapshot)
            self.assertEqual(key, event)
            return httpx.Response(200, json={"accepted": True, "duplicate": True, "external_order_id": "MES-1"})
        order_webhook.tick(sender)
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT state FROM order_webhook_actions").fetchone()[0], "sent")

    def test_rework_without_instance_and_unlinked_manager_file(self):
        iid, event = self.fixture()
        with db.transaction() as con:
            con.execute("DELETE FROM order_webhook_actions")
            con.execute("UPDATE outbox SET instance_id=NULL WHERE dedup='sent-preview'")
            row = order_webhook.enqueue(con, iid, 'hook')
            self.assertIsNotNone(row['image_job_id'])
            con.execute("DELETE FROM order_webhook_actions")
            instance = con.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
            mid = self.image(con, instance['chat_id'], instance['item_id'])
            con.execute("INSERT INTO outbox(chat_id,actor,body,dedup,epoch,state,created_at,kind,media_id) VALUES(?,'manager:1','','new-manual',0,'sent',?,'file',?)", (instance['chat_id'], db.now(), mid))
            row = order_webhook.enqueue(con, iid, 'hook')
            self.assertIsNone(row['image_job_id'])

    def row(self):
        with db.transaction() as con:
            return con.execute("SELECT * FROM order_webhook_actions").fetchone()

    def test_timeout_restart_and_backoff_are_durable(self):
        _, event = self.fixture()
        calls = []
        def timeout(url, token, payload, key):
            calls.append((payload, key))
            raise httpx.ReadTimeout("timeout")
        for attempt, minutes in enumerate((1, 2, 5, 15, 30, 60, 60), 1):
            order_webhook.tick(timeout)
            row = self.row()
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["attempts"], attempt)
            delta = datetime.fromisoformat(row["due_at"]) - datetime.fromisoformat(row["last_attempt_at"])
            self.assertAlmostEqual(delta.total_seconds(), minutes * 60, delta=3)
            db.initialize()
            with db.transaction() as con:
                con.execute("UPDATE order_webhook_actions SET due_at='2000-01-01'")
        self.assertTrue(all(call == calls[0] for call in calls))
        order_webhook.tick(lambda *a: httpx.Response(200, json={"accepted": True, "duplicate": True, "external_order_id": "MES-42"}))
        self.assertEqual(self.row()["external_order_id"], "MES-42")
        self.assertEqual(self.row()["state"], "sent")

    def test_response_matrix_and_no_html_success(self):
        self.fixture()
        for code, body, state in (
            (400, {"message": "Ошибка данных"}, "failed"),
            (422, {"message": "Магазин не существует"}, "failed"),
            (409, {"message": "Позиция изменена"}, "failed"),
            (502, {}, "pending"),
            (503, {}, "pending"),
            (200, {"accepted": False}, "pending"),
            (202, {"accepted": True}, "pending"),
            (200, {"accepted": True, "external_order_id": 123}, "pending"),
        ):
            with self.subTest(code=code, body=body):
                with db.transaction() as con:
                    con.execute("UPDATE order_webhook_actions SET state='pending',delivery_only=1,due_at='2000-01-01'")
                order_webhook.tick(lambda *a: httpx.Response(code, json=body))
                self.assertEqual(self.row()["state"], state)
                self.assertEqual(self.row()["http_code"], code)
        with db.transaction() as con:
            con.execute("UPDATE order_webhook_actions SET due_at='2000-01-01'")
        order_webhook.tick(lambda *a: httpx.Response(200, text="<html>OK</html>"))
        self.assertEqual(self.row()["state"], "pending")
        self.assertIsNone(self.row()["external_order_id"])

    def test_auth_and_configuration_pause_until_settings_fixed(self):
        self.fixture()
        for code, body in ((401, {"message": "Неверный токен"}),
                           (503, {"code": "CONFIGURATION_ERROR", "message": "Настройте MES"})):
            with self.subTest(code=code):
                with db.transaction() as con:
                    con.execute("UPDATE order_webhook_actions SET state='pending',delivery_only=1,due_at='2000-01-01'")
                order_webhook.tick(lambda *a: httpx.Response(code, json=body))
                self.assertEqual(self.row()["state"], "failed")
                self.assertFalse(order_webhook.tick(lambda *a: self.fail("Paused integration")))
                with db.transaction() as con:
                    self.assertTrue(order_webhook.config(con)["paused_reason"])
                    order_webhook.save(con, self.webhook_settings())
                    self.assertFalse(order_webhook.config(con).get("paused_reason"))

    def test_conflict_requires_reconciliation_and_reuses_event(self):
        iid, event = self.fixture()
        order_webhook.tick(lambda *a: httpx.Response(409, json={"message": "Conflict"}))
        snapshot = self.row()["payload"]
        self.assertEqual(self.client.post(f"/settings/order-webhook/{event}/retry", headers=self.admin,
                         data={"csrf": "csrf"}).status_code, 422)
        self.assertEqual(self.client.post(f"/settings/order-webhook/{event}/retry", headers=self.admin,
                         data={"csrf": "csrf", "reconciled": "yes"}, follow_redirects=False).status_code, 303)
        self.assertEqual(self.row()["attempts"], 1)
        with db.transaction() as con:
            self.assertEqual(order_webhook.enqueue(con, iid, "another-node")["id"], event)
        def sent(url, token, payload, key):
            self.assertEqual(payload, snapshot)
            self.assertEqual(key, event)
            return httpx.Response(202, json={"accepted": True, "external_order_id": "X"})
        order_webhook.tick(sent)
        with db.transaction() as con:
            self.assertEqual(order_webhook.enqueue(con, iid, "yet-another-node")["id"], event)

    def test_legacy_events_are_never_replayed(self):
        iid, event = self.fixture()
        with db.transaction() as con:
            con.execute("UPDATE order_webhook_actions SET protocol=1,payload=?",
                        (db.dump({"order": {"seller_account_id": "1"}}),))
        db.initialize()
        self.assertEqual(self.row()["state"], "failed")
        self.assertFalse(order_webhook.tick(lambda *a: self.fail("legacy")))
        self.assertEqual(self.client.post(f"/settings/order-webhook/{event}/retry", headers=self.admin,
                         data={"csrf": "csrf"}).status_code, 422)

    def test_no_webhook_when_production_save_fails(self):
        self.fixture()
        with patch("app.order_webhook.os.fsync", side_effect=OSError("disk full")):
            order_webhook.tick(lambda *a: self.fail("File not saved"))
        self.assertEqual(self.row()["state"], "failed")
        self.assertEqual(self.row()["attempts"], 0)
        self.assertIsNone(self.row()["payload"])

    def test_payload_limits_and_shop_validation(self):
        self.fixture()
        with db.transaction() as con:
            action = con.execute("SELECT * FROM order_webhook_actions").fetchone()
            instance = con.execute("SELECT * FROM instances").fetchone()
            item = dict(con.execute("SELECT * FROM items WHERE id=?", (instance["item_id"],)).fetchone())
            job = dict(con.execute("SELECT * FROM image_jobs").fetchone())
            settings = order_webhook.config(con)
        for key, value in (("offer_id", ""), ("posting", "a" * 256), ("quantity", 1.5), ("quantity", 0)):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    order_webhook.prepare(action, settings, {**item, key: value}, job)
        for value in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                order_webhook.prepare(action, settings, item, {**job, "width_cm": value})
        with db.transaction() as con:
            for value in ("-1", "0", "1.5", "abc"):
                with self.assertRaises(ValueError):
                    order_webhook.save(con, {**self.webhook_settings(), "shop_1": value})

    def test_multiple_items_and_shops_keep_identity(self):
        _, event = self.fixture()
        with db.transaction() as con:
            action = dict(con.execute("SELECT * FROM order_webhook_actions").fetchone())
            instance = con.execute("SELECT * FROM instances").fetchone()
            item = dict(con.execute("SELECT * FROM items WHERE id=?", (instance["item_id"],)).fetchone())
            job = con.execute("SELECT * FROM image_jobs").fetchone()
            settings = order_webhook.config(con)
        import uuid
        payloads = []
        for item_id, account_id, shop in ((1, 1, 12), (2, 1, 12), (3, 2, 19)):
            payloads.append(order_webhook.prepare(
                {**action, "id": str(uuid.uuid4())},
                {**settings, "shops": {"1": 12, "2": 19}},
                {**item, "id": item_id, "account_id": account_id, "quantity": 2}, job))
        self.assertEqual(payloads[0]["order"]["posting_number"], payloads[1]["order"]["posting_number"])
        self.assertEqual([p["order"]["shop_id"] for p in payloads], [12, 12, 19])
        self.assertEqual(len({p["order"]["item_id"] for p in payloads}), 3)
        self.assertEqual(len({p["print_file"]["id"] for p in payloads}), 3)

    def test_diagnostics_do_not_expose_token(self):
        self.fixture()
        token = "s" * 32
        order_webhook.tick(lambda *a: httpx.Response(422, json={"message": "Invalid Bearer " + token}))
        self.assertNotIn(token, self.row()["error"])
