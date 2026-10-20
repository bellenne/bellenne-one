import io
import json
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
from PIL import Image
from app import db, image_tasks, order_webhook, placement, scenarios
from tests import test_folio, test_order_webhook


class PlacementTest(unittest.TestCase):
    setUp = test_folio.FolioTest.setUp
    tearDown = test_folio.FolioTest.tearDown
    setup_instance = test_folio.FolioTest.setup_instance
    image = test_folio.FolioTest.image
    image_worker_settings = test_folio.FolioTest.image_worker_settings
    fixture = test_order_webhook.OrderWebhookTest.fixture
    webhook_settings = test_order_webhook.OrderWebhookTest.webhook_settings

    def headers(self, owner=None, session=None):
        return {"Authorization": "Bearer KEY", "X-Folio-Worker-Id": owner or self.owner,
                "X-Folio-Session-Id": session or self.session}

    def init_worker(self):
        self.owner, self.session = str(uuid.uuid4()), str(uuid.uuid4())
        self.client.post("/settings/image-worker", headers=self.admin,
                         data={"csrf": "csrf", "api_key": "KEY"})

    def files(self, count=2):
        return [{"serial": n, "instance_code": f"copy-{n}", "layout_key": f"art-{n}", "filename": f"art-{n}.png"}
                for n in range(1, count + 1)]

    def accepted(self, count=2):
        return httpx.Response(202, json={"accepted": True, "external_order_id": "MES-123",
                                        "print_files": self.files(count)})

    def prepared(self):
        self.init_worker()
        instance, event = self.fixture()
        with db.transaction() as con:
            con.execute("UPDATE image_jobs SET owner_id=?,worker_id='generation-id'", (self.owner,))
            con.execute("UPDATE items SET quantity=2")
        self.assertTrue(order_webhook.tick(lambda *args: self.accepted()))
        job = self.client.get("/image-worker/next", headers=self.headers()).json()
        self.assertEqual(job["action"], "place_print_files")
        return instance, event, job

    def report(self, job, files, status="finished", headers=None):
        return self.client.post(f"/image-worker/jobs/{job['id']}", headers=headers or self.headers(), json={
            "action": "place_print_files", "operation_id": job["operation_id"],
            "layout_version": job["layout_version"], "claim_token": job["claim_token"],
            "status": status, "files": files})

    def success(self, serial):
        return {"serial": serial, "filename": f"art-{serial}.png", "status": "completed",
                "saved_path": rf"\\production\prints\art-{serial}.png", "size_bytes": 100, "sha256": "a" * 64}

    def test_partial_retry_restart_and_stale_claim(self):
        _, _, job = self.prepared()
        self.assertEqual(self.client.get("/image-worker/next", headers=self.headers()).json()["claim_token"], job["claim_token"])
        wrong = self.headers(owner=str(uuid.uuid4()))
        self.assertEqual(self.client.get("/image-worker/next", headers=wrong).status_code, 204)
        self.assertEqual(self.report(job, [self.success(1)], headers=wrong).status_code, 409)
        failed = {"serial": 2, "filename": "art-2.png", "status": "failed", "error": "Нет доступа к папке"}
        response = self.report(job, [self.success(1), failed])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["placement_state"], "partial")
        page = self.client.get("/settings/order-webhook", headers=self.admin)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Частично выполнено", page.text)
        self.assertIn("Нет доступа к папке", page.text)
        self.assertEqual(self.report(job, [self.success(1), failed]).status_code, 200)
        db.initialize()
        self.session = str(uuid.uuid4())  # restarted worker cannot take an active old lease
        self.assertEqual(self.client.get("/image-worker/next", headers=self.headers()).status_code, 204)
        with db.transaction() as con:
            con.execute("UPDATE image_jobs SET placement_until='2000',placement_due='2000'")
        retry = self.client.get("/image-worker/next", headers=self.headers()).json()
        self.assertEqual(retry["pending_serials"], [2])
        self.assertNotEqual(retry["claim_token"], job["claim_token"])
        self.assertEqual(retry["operation_id"], job["operation_id"])
        self.assertEqual(self.report(job, [self.success(2)]).status_code, 409)
        done = self.report(retry, [self.success(2)])
        self.assertEqual(done.json()["placement_state"], "completed", done.text)
        self.assertEqual(self.report(retry, [self.success(2)]).status_code, 200)
        self.assertEqual(self.report(retry, [failed]).status_code, 409)
        self.assertEqual(self.client.get("/image-worker/next", headers=self.headers()).status_code, 204)
        with db.transaction() as con:
            self.assertEqual(len(json.loads(con.execute("SELECT placement_results FROM image_jobs").fetchone()[0])), 2)

    def test_competing_sessions_and_changed_version(self):
        instance, _, job = self.prepared()
        with db.transaction() as con:
            con.execute("UPDATE image_jobs SET placement_until='2000'")
        def poll(session):
            with db.transaction() as con:
                return placement.claim(con, self.owner, session)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(poll, [str(uuid.uuid4()), str(uuid.uuid4())]))
        self.assertEqual(sum(result is not None for result in results), 1)
        claimed = next(result for result in results if result)
        with db.transaction() as con:
            con.execute("UPDATE instances SET approved_image_job_id=NULL WHERE id=?", (instance,))
            self.session = con.execute("SELECT placement_session FROM image_jobs").fetchone()[0]
        self.assertEqual(self.report(claimed, [self.success(1)]).status_code, 409)
        self.assertEqual(self.client.get("/image-worker/next", headers=self.headers()).status_code, 204)
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT placement_state FROM image_jobs").fetchone()[0], "stale")

    def test_report_validation_does_not_change_stored_success(self):
        _, _, job = self.prepared()
        bad = {**self.success(1), "saved_path": r"\\production\wrong.png"}
        self.assertEqual(self.report(job, [bad]).status_code, 409)
        bad = {**self.success(1), "sha256": "not-a-hash"}
        self.assertEqual(self.report(job, [bad]).status_code, 409)
        self.assertEqual(self.report({**job, "layout_version": "old-version"}, [self.success(1)]).status_code, 409)
        self.assertEqual(self.report(job, [self.success(1), self.success(1)]).status_code, 409)
        self.assertEqual(self.report(job, [self.success(1)], status="running").status_code, 200)
        self.assertEqual(self.report(job, [{**self.success(1), "size_bytes": 101}]).status_code, 409)
        with db.transaction() as con:
            result = json.loads(con.execute("SELECT placement_results FROM image_jobs").fetchone()[0])
            self.assertEqual(result["1"]["size_bytes"], 100)

    def test_invalid_mes_names_and_retry_unchanged_payload(self):
        self.init_worker()
        _, event = self.fixture()
        with db.transaction() as con:
            con.execute("UPDATE image_jobs SET owner_id=?", (self.owner,))
        sent = []
        def invalid(url, token, payload, key):
            sent.append((payload, key))
            return httpx.Response(200, json={"accepted": True, "external_order_id": "MES-123"})
        order_webhook.tick(invalid)
        self.assertEqual(self.client.get("/image-worker/next", headers=self.headers()).status_code, 204)
        with db.transaction() as con:
            row = con.execute("SELECT * FROM order_webhook_actions").fetchone()
            self.assertEqual(row["state"], "sent")
            self.assertTrue(row["placement_error"])
        response = self.client.post(f"/settings/order-webhook/{event}/retry", headers=self.admin,
                                    data={"csrf": "csrf"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        def valid(url, token, payload, key):
            sent.append((payload, key))
            return self.accepted(1)
        order_webhook.tick(valid)
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(json.loads(sent[1][0])["print_file"]["filename"], "print.png")
        self.assertEqual(self.client.get("/image-worker/next", headers=self.headers()).json()["print_files"], self.files(1))
        for invalid_files in ([], [{"serial": 1}], self.files(2), [{**self.files(1)[0], "filename": "../art-1.png"}]):
            with self.assertRaises(ValueError):
                placement.validate_files(invalid_files, 1)

    def test_missing_owner_and_unapproved_blocked(self):
        self.init_worker()
        instance, _ = self.fixture()
        with db.transaction() as con:
            con.execute("UPDATE instances SET approved_image_job_id=NULL WHERE id=?", (instance,))
        order_webhook.tick(lambda *args: self.fail("No MES before approval"))
        with db.transaction() as con:
            con.execute("UPDATE instances SET approved_image_job_id=(SELECT id FROM image_jobs) WHERE id=?", (instance,))
            con.execute("UPDATE order_webhook_actions SET state='pending',due_at='2000',delivery_only=1")
        order_webhook.tick(lambda *args: self.accepted(1))
        self.assertEqual(self.client.get("/image-worker/next", headers=self.headers()).status_code, 204)
        with db.transaction() as con:
            self.assertIn("владелец", con.execute("SELECT placement_error FROM order_webhook_actions").fetchone()[0])

    def test_generation_approval_mes_placement_end_to_end(self):
        self.init_worker()
        instance, item, chat, _ = self.setup_instance()
        graph = {"nodes": [
            {"id": "make", "kind": "image_worker", "prompt": "Портрет", "next": "send", "error": "end"},
            {"id": "send", "kind": "send_mockup", "next": "approve"},
            {"id": "approve", "kind": "approval", "dictionary_version": 1, "text": "Согласуйте", "hours": 6,
             "next": "mes", "otherwise": "end", "timeout": "end", "error": "end"},
            {"id": "mes", "kind": "order_webhook", "next": "end", "error": "end"},
            {"id": "end", "kind": "end"}]}
        with db.transaction() as con:
            order_webhook.save(con, self.webhook_settings())
            con.execute("UPDATE items SET order_number='ORDER-1' WHERE id=?", (item,))
            mid = self.image(con, chat, item)
            con.execute("UPDATE versions SET graph=? WHERE id=(SELECT version_id FROM instances WHERE id=?)",
                        (db.dump(graph), instance))
            con.execute("UPDATE instances SET node='make',prompted=0,fields=? WHERE id=?",
                        (db.dump({"photos": [mid]}), instance))
            scenarios.advance(con, instance)
        job = self.client.get("/image-worker/next", headers=self.headers()).json()
        self.assertEqual(job["action"], "generate")
        preview = io.BytesIO()
        Image.new("RGB", (667, 1000)).save(preview, format="JPEG")
        response = self.client.post(f"/image-worker/jobs/{job['id']}", headers=self.headers(),
            data={"worker_id": job["worker_id"], "status": "completed", "print_file": "D:/Results/original.jpg"},
            files={"preview": ("preview.jpg", preview.getvalue(), "image/jpeg")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(order_webhook.tick(lambda *args: self.fail("No approval yet")))
        with db.transaction() as con:
            outgoing = con.execute("SELECT id FROM outbox WHERE kind='file' ORDER BY id DESC LIMIT 1").fetchone()[0]
            con.execute("UPDATE outbox SET state='sent' WHERE id=?", (outgoing,))
            scenarios.outgoing_confirmed(con, outgoing)
            con.execute("UPDATE outbox SET state='sent' WHERE instance_id=?", (instance,))
            scenarios.advance(con, instance, {"text": "Да"})
            state = dict(con.execute("SELECT node,status,fields FROM instances WHERE id=?", (instance,)).fetchone())
            self.assertEqual(state["node"], "mes", state)
        self.assertTrue(order_webhook.tick(lambda *args: self.accepted(1)))
        operation = self.client.get("/image-worker/next", headers=self.headers()).json()
        self.assertEqual(operation["source_print_file"], "D:/Results/original.jpg")
        self.assertEqual(operation["id"], job["id"])
        self.assertEqual(self.report(operation, [self.success(1)]).json()["placement_state"], "completed")
