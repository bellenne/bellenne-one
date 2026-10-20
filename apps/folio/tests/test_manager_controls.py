import unittest
from unittest.mock import patch

from app import db, scenarios
from tests import test_folio


class ManagerControlsTest(unittest.TestCase):
    setUp = test_folio.FolioTest.setUp
    tearDown = test_folio.FolioTest.tearDown
    setup_instance = test_folio.FolioTest.setup_instance
    image = test_folio.FolioTest.image

    def setup_approval(self):
        instance, item, chat, scenario = self.setup_instance()
        graph = {"nodes": [
            {"id": "approve", "kind": "approval", "dictionary_version": 1, "text": "Согласны?",
             "hours": 6, "next": "end", "otherwise": "end", "timeout": "end", "error": "end"},
            {"id": "end", "kind": "end"}]}
        with db.transaction() as con:
            con.execute("INSERT INTO assignments VALUES(?,'2')", (chat,))
            con.execute("UPDATE versions SET graph=? WHERE id=(SELECT version_id FROM instances WHERE id=?)",
                        (db.dump(graph), instance))
            con.execute("UPDATE instances SET node='approve',status='waiting_approval',prompted=1,"
                        "approval_due_at='2026-10-20T12:00:00+00:00' WHERE id=?", (instance,))
            con.execute("UPDATE outbox SET state='sent'")
        return instance, item, chat, scenario

    def control(self, instance, action, stamp="2026-10-20T10:00:00+00:00", **extra):
        with patch("app.db.now", return_value=stamp):
            return self.client.post(f"/instances/{instance}/approval-timer", headers=self.manager,
                data={"csrf": "csrf", "action": action, **extra}, follow_redirects=False)

    def record(self, instance):
        with db.transaction() as con:
            return dict(con.execute("SELECT * FROM instances WHERE id=?", (instance,)).fetchone())

    def test_pause_resume_persists_and_preserves_remaining_time(self):
        instance, _, chat, _ = self.setup_approval()
        self.assertEqual(self.control(instance, "pause", return_to="orders").headers["location"], "/folio/orders")
        self.assertEqual(self.record(instance)["approval_remaining_seconds"], 7200)
        self.assertIsNone(self.record(instance)["approval_due_at"])
        self.assertEqual(self.control(instance, "pause", stamp="2026-10-20T15:00:00+00:00").status_code, 303)
        db.initialize()
        with patch("app.db.now", return_value="2026-10-20T15:00:00+00:00"), db.transaction() as con:
            scenarios.expire_approvals(con)
        self.assertEqual(self.record(instance)["status"], "waiting_approval")
        for path in ("/orders", f"/chats/{chat}"):
            page = self.client.get(path, headers=self.manager)
            self.assertEqual(page.status_code, 200)
            self.assertIn("Продолжить отсчёт", page.text)
        self.assertEqual(self.control(instance, "resume", stamp="2026-10-20T15:00:00+00:00").status_code, 303)
        self.assertEqual(self.record(instance)["approval_due_at"], "2026-10-20T17:00:00+00:00")
        self.control(instance, "resume", stamp="2026-10-20T16:00:00+00:00")
        self.assertEqual(self.record(instance)["approval_due_at"], "2026-10-20T17:00:00+00:00")

    def test_business_hours_and_customer_reply_while_paused(self):
        instance, _, _, _ = self.setup_approval()
        with db.transaction() as con:
            settings = db.config(con)
            settings.update(business_hours_enabled=True, business_start="09:00", business_end="18:00")
            con.execute("UPDATE settings SET value=? WHERE id=1", (db.dump(settings),))
            con.execute("UPDATE instances SET approval_due_at='2026-10-20T15:00:00+00:00'")
        self.control(instance, "pause", stamp="2026-10-20T14:30:00+00:00")
        self.control(instance, "resume", stamp="2026-10-20T16:00:00+00:00")
        self.assertEqual(self.record(instance)["approval_due_at"], "2026-10-21T06:30:00+00:00")
        self.control(instance, "pause", stamp="2026-10-20T16:00:00+00:00")
        with db.transaction() as con:
            scenarios.advance(con, instance, {"text": "Да"})
        self.assertEqual(self.record(instance)["status"], "closed")
        self.assertIsNone(self.record(instance)["approval_remaining_seconds"])
        self.assertEqual(self.control(instance, "resume").status_code, 422)

    def test_manager_resume_pinned_step_without_setting_and_access_scope(self):
        instance, _, chat, _ = self.setup_approval()
        other, _, other_chat, _ = self.setup_instance(posting="FOREIGN", external_chat="OTHER")
        with db.transaction() as con:
            settings = db.config(con)
            settings["manager_resume"] = False
            con.execute("UPDATE settings SET value=? WHERE id=1", (db.dump(settings),))
            scenarios.takeover(con, chat, "2")
        page = self.client.get(f"/chats/{chat}", headers=self.manager)
        self.assertIn("Шаг возобновления", page.text)
        self.assertEqual(self.client.post(f"/chats/{chat}/resume", headers=self.manager,
            data={"csrf": "csrf", "node": "not-in-version"}).status_code, 422)
        response = self.client.post(f"/chats/{chat}/resume", headers=self.manager,
            data={"csrf": "csrf", "node": "end"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.record(instance)["node"], "end")
        self.assertIsNone(self.record(instance)["approval_due_at"])
        self.assertEqual(self.control(other, "pause").status_code, 403)
        self.assertEqual(self.client.post(f"/chats/{other_chat}/resume", headers=self.manager,
            data={"csrf": "csrf", "node": "end"}).status_code, 403)
        orders = self.client.get("/orders", headers=self.manager)
        self.assertEqual(orders.status_code, 200)
        self.assertNotIn("FOREIGN", orders.text)
        self.assertEqual(self.client.get("/image-jobs", headers=self.manager).status_code, 200)
        for path in ("/settings", "/scenarios", "/audit", f"/briefs/{instance}/export"):
            self.assertEqual(self.client.get(path, headers=self.manager).status_code, 403)
        self.assertEqual(self.client.post("/image-jobs", headers=self.manager, data={"csrf": "csrf"}).status_code, 403)

    def test_timer_rejects_cancelled_unsent_and_csrf(self):
        instance, item, _, _ = self.setup_approval()
        endpoint = f"/instances/{instance}/approval-timer"
        self.assertEqual(self.client.post(endpoint, headers=self.manager, data={"action": "pause"}).status_code, 403)
        with db.transaction() as con:
            con.execute("UPDATE instances SET approval_due_at=NULL")
        self.assertEqual(self.control(instance, "pause").status_code, 422)
        with db.transaction() as con:
            con.execute("UPDATE items SET external_status='cancelled' WHERE id=?", (item,))
        self.assertEqual(self.control(instance, "resume").status_code, 422)

    def test_manager_task_list_and_preview_are_scoped(self):
        _, item, chat, _ = self.setup_approval()
        _, other_item, other_chat, _ = self.setup_instance(posting="PRIVATE-ORDER", external_chat="PRIVATE-CHAT")
        jobs = []
        with db.transaction() as con:
            for iid, cid in ((item, chat), (other_item, other_chat)):
                mid = self.image(con, cid, iid)
                jobs.append(con.execute(
                    "INSERT INTO image_jobs(item_id,media_id,preview_media_id,prompt,width_cm,height_cm,state,"
                    "print_file,created_at,updated_at) VALUES(?,?,?,'prompt',40,60,'completed','D:/private/file.jpg',?,?)",
                    (iid, mid, mid, db.now(), db.now())).lastrowid)
        page = self.client.get("/image-jobs", headers=self.manager)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("PRIVATE-ORDER", page.text)
        self.assertNotIn("D:/private", page.text)
        self.assertNotIn('href="/folio/settings', page.text)
        self.assertEqual(self.client.get(f"/image-jobs/{jobs[0]}/preview", headers=self.manager).status_code, 200)
        self.assertEqual(self.client.get(f"/image-jobs/{jobs[1]}/preview", headers=self.manager).status_code, 403)
