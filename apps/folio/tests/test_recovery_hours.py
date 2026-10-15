import unittest
from unittest.mock import patch

import test_folio
from app import business_hours as hours, db, image_tasks, scenarios, worker


class RecoveryHoursTest(unittest.TestCase):
    setUp = test_folio.FolioTest.setUp
    tearDown = test_folio.FolioTest.tearDown
    setup_instance = test_folio.FolioTest.setup_instance
    image = test_folio.FolioTest.image

    def schedule(self):
        return {**self.settings, "business_hours_enabled": True,
                "business_start": "09:00", "business_end": "18:00"}

    def test_daily_and_overnight_clocks(self):
        config = self.schedule()
        self.assertFalse(hours.is_open(config, "2026-10-15T05:59:00+00:00"))
        self.assertTrue(hours.is_open(config, "2026-10-15T06:00:00+00:00"))
        self.assertFalse(hours.is_open(config, "2026-10-15T15:00:00+00:00"))
        self.assertEqual(hours.add_seconds(config, "2026-10-15T14:00:00+00:00", 6*3600),
                         "2026-10-16T11:00:00+00:00")
        self.assertEqual(hours.add_seconds(config, "2026-10-15T20:00:00+00:00", 6*3600),
                         "2026-10-16T12:00:00+00:00")
        self.assertEqual(hours.seconds_between(config, "2026-10-15T14:00:00+00:00", "2026-10-16T11:00:00+00:00"), 21600)
        overnight = {**config, "business_start": "22:00", "business_end": "06:00"}
        self.assertTrue(hours.is_open(overnight, "2026-10-15T23:00:00+00:00"))
        self.assertEqual(hours.add_seconds(overnight, "2026-10-15T22:00:00+00:00", 6*3600),
                         "2026-10-16T20:00:00+00:00")
        with self.assertRaises(ValueError):
            hours.validate({**config, "business_end": "09:00"})

    def test_night_outbox_waits_but_manager_can_send_and_morning_resumes(self):
        _, _, chat, _ = self.setup_instance()
        _, _, manual_chat, _ = self.setup_instance(posting="SECOND", external_chat="OTHER")
        with db.transaction() as con:
            con.execute("UPDATE settings SET value=?", (db.dump(self.schedule()),))
            con.execute("DELETE FROM outbox WHERE chat_id=?", (manual_chat,))
            row = con.execute("SELECT * FROM chats WHERE id=?", (manual_chat,)).fetchone()
            scenarios.queue_text(con, row, None, "Ручной ответ", "manual-check", "manager:1")
        sent = []
        class Adapter:
            def __init__(self, *_args): pass
            def send(self, chat, text):
                sent.append(text)
                return "id-" + str(len(sent))
            def close(self): pass
        with patch.object(db, "now", return_value="2026-10-15T20:00:00+00:00"):
            self.assertTrue(worker.send_one(Adapter))
            self.assertEqual(sent, ["Ручной ответ"])
            self.assertFalse(worker.send_one(Adapter))
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT state FROM outbox WHERE chat_id=?", (chat,)).fetchone()[0], "pending")
        with patch.object(db, "now", return_value="2026-10-16T06:00:00+00:00"):
            self.assertTrue(worker.send_one(Adapter))
        self.assertEqual(len(sent), 2)

    def test_schedule_settings_permissions_validation_and_existing_timer(self):
        iid, _, _, _ = self.setup_instance()
        with db.transaction() as con:
            con.execute("UPDATE instances SET status='waiting_approval',approval_due_at='2026-10-16T02:00:00+00:00' WHERE id=?", (iid,))
        payload = {"csrf": "csrf", "business_hours_enabled": "on", "business_start": "09:00",
                   "business_end": "18:00", "timer_origin": "confirmation", "manager_scope": "assigned"}
        self.assertEqual(self.client.post('/settings/automation', headers=self.manager, data=payload).status_code, 403)
        self.assertEqual(self.client.post('/settings/automation', headers=self.admin,
                         data={**payload, 'business_start': 'bad'}).status_code, 422)
        with patch.object(db, "now", return_value="2026-10-15T20:00:00+00:00"):
            result = self.client.post('/settings/automation', headers=self.admin, data=payload, follow_redirects=False)
            self.assertEqual(result.status_code, 303, result.text)
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT approval_due_at FROM instances WHERE id=?", (iid,)).fetchone()[0],
                             "2026-10-16T12:00:00+00:00")
        self.assertIn('type="time"', self.client.get('/settings/automation', headers=self.admin).text)

    def test_approval_timer_starts_on_confirmed_send_and_excludes_night(self):
        iid, _, chat, _ = self.setup_instance()
        with db.transaction() as con:
            con.execute("UPDATE settings SET value=?", (db.dump(self.schedule()),))
            version = con.execute("SELECT version_id FROM instances WHERE id=?", (iid,)).fetchone()[0]
            con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump({"nodes": [
                {"id": "approval", "kind": "approval", "hours": 6}]}), version))
            con.execute("UPDATE instances SET node='approval',status='waiting_approval' WHERE id=?", (iid,))
            con.execute("UPDATE outbox SET dedup='test:approval',state='pending' WHERE chat_id=?", (chat,))
            outgoing = con.execute("SELECT id FROM outbox WHERE chat_id=?", (chat,)).fetchone()[0]
            with patch.object(db, "now", return_value="2026-10-15T14:00:00+00:00"):
                scenarios.outgoing_confirmed(con, outgoing)
                self.assertIsNone(con.execute("SELECT approval_due_at FROM instances WHERE id=?", (iid,)).fetchone()[0])
                con.execute("UPDATE outbox SET state='sent' WHERE id=?", (outgoing,))
                scenarios.outgoing_confirmed(con, outgoing)
            due = con.execute("SELECT approval_due_at FROM instances WHERE id=?", (iid,)).fetchone()[0]
            self.assertEqual(due, "2026-10-16T11:00:00+00:00")
            with patch.object(db, "now", return_value="2026-10-15T20:00:00+00:00"):
                scenarios.outgoing_confirmed(con, outgoing)
            self.assertEqual(con.execute("SELECT approval_due_at FROM instances WHERE id=?", (iid,)).fetchone()[0], due)

    def test_manager_retry_unknown_is_explicit_idempotent_and_isolated(self):
        _, item, chat, _ = self.setup_instance()
        _, other_item, other_chat, _ = self.setup_instance(posting="OTHER", external_chat="OTHER")
        with db.transaction() as con:
            con.execute("INSERT INTO assignments(chat_id,user_id) VALUES(?,'2')", (chat,))
            mid = self.image(con, chat, item)
            jid = con.execute("INSERT INTO image_jobs(item_id,media_id,media_ids,prompt,width_cm,height_cm,state,"
                              "worker_id,pull_claimed,created_at,updated_at,node_id,chat_id) "
                              "VALUES(?,?,'[]','Exact prompt',40,60,'unknown','old-worker',1,?,?,'rework:old',?)",
                              (item, mid, db.now(), db.now(), chat)).lastrowid
        payload = {"csrf": "csrf", "job_id": str(jid)}
        self.assertEqual(self.client.post(f'/chats/{chat}/rework_retry', headers=self.manager, data=payload).status_code, 422)
        self.assertEqual(self.client.post(f'/chats/{other_chat}/rework_retry', headers=self.manager, data=payload).status_code, 403)
        payload['acknowledge_unknown'] = 'on'
        for _ in range(2):
            result = self.client.post(f'/chats/{chat}/rework_retry', headers=self.manager, data=payload, follow_redirects=False)
            self.assertEqual(result.status_code, 303, result.text)
        with db.transaction() as con:
            jobs = con.execute("SELECT * FROM image_jobs ORDER BY id").fetchall()
            self.assertEqual(len(jobs), 2)
            self.assertEqual(jobs[0]['state'], 'failed')
            self.assertEqual(jobs[1]['state'], 'pending')
            self.assertEqual(jobs[1]['prompt'], 'Exact prompt')
            self.assertIsNone(jobs[1]['worker_id'])
            with self.assertRaises(image_tasks.ImageTaskError):
                image_tasks.receive_update(con, jid, 'old-worker', 'completed', 'old.tif', preview_bytes=b'late')
            con.execute("UPDATE image_jobs SET state='unknown' WHERE id=?", (jobs[1]['id'],))
            next_id = jobs[1]['id']
        payload['job_id'] = str(next_id)
        self.assertIn('Повторить доработку', self.client.get(f'/chats/{chat}', headers=self.manager).text)
        self.assertEqual(self.client.post(f'/chats/{chat}/rework_close', headers=self.manager,
                         data=payload, follow_redirects=False).status_code, 303)
        with db.transaction() as con:
            self.assertFalse(con.execute("SELECT 1 FROM image_jobs WHERE state NOT IN ('completed','failed')").fetchone())
