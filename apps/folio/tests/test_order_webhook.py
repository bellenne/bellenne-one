import json
import unittest
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
        return {"csrf": "csrf", "url": "https://laravel.example.com/api/folio",
                "public_url": "https://folio.example.com/folio", "send_token": "s" * 32,
                "download_token": "d" * 32, "worker_root": "D:/Print", "mount_root": str(db.DATA / "mount")}

    def fixture(self):
        iid, item, chat, sid = self.setup_instance()
        root = db.DATA / "mount"
        root.mkdir()
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
            return httpx.Response(503 if len(calls) == 1 else 202, json={"accepted": True})
        order_webhook.tick(sender)
        with db.transaction() as con:
            row = con.execute("SELECT * FROM order_webhook_actions").fetchone()
            self.assertEqual(row["state"], "pending")
            con.execute("UPDATE order_webhook_actions SET due_at='2000-01-01'")
        (db.DATA / "mount/print.png").write_bytes(b"changed")
        order_webhook.tick(sender)
        self.assertEqual(calls[0], calls[1])
        payload = json.loads(calls[0][0])
        self.assertEqual(payload["order"]["type"], "Картина")
        self.assertEqual(payload["order"]["width_cm"], 40)
        self.assertNotIn("approval_outcome", payload["order"])
        self.assertEqual(self.client.get(f"/integration-files/{event}").status_code, 401)
        result = self.client.get(f"/integration-files/{event}", headers={"Authorization": "Bearer " + "d" * 32})
        self.assertEqual(result.content, b"immutable-print")
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
        order_webhook.tick(lambda *a: httpx.Response(202, json={"accepted": True}))
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

    def test_http_auth_redirect_not_followed(self):
        def handler(request):
            self.assertEqual(request.headers["Idempotency-Key"], "event")
            self.assertEqual(request.headers["Authorization"], "Bearer secret")
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
            return httpx.Response(200, json={"accepted": True, "duplicate": True})
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
