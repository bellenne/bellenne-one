import json
import unittest

import httpx

from app import db, order_webhook, placement, scenarios
from tests import test_folio, test_manager_controls


class MockupChoiceTest(unittest.TestCase):
    Base = test_manager_controls.ManagerControlsTest
    setUp = test_folio.FolioTest.setUp
    tearDown = test_folio.FolioTest.tearDown
    setup_instance = test_folio.FolioTest.setup_instance
    image = test_folio.FolioTest.image
    setup_approval = Base.setup_approval
    setup_mes = Base.setup_mes
    sent_file = Base.sent_file
    resume = Base.resume
    check = Base.check
    action = Base.action
    tick = Base.tick
    record = Base.record

    def three_mockups(self):
        """Mockup 1 was sent to the buyer; the two newer ones were not."""
        instance, item, chat, first = self.setup_mes()
        jobs = [first]
        with db.transaction() as con:
            for number in (2, 3):
                mid = self.image(con, chat, item)
                jobs.append(con.execute(
                    "INSERT INTO image_jobs(item_id,media_id,preview_media_id,prompt,width_cm,height_cm,state,print_file,"
                    "owner_id,created_at,updated_at) VALUES(?,?,?,'p',40,60,'completed',?,'owner',?,?)",
                    (item, mid, mid, f"D:/Print/m{number}.jpg", db.now(), db.now())).lastrowid)
        return instance, item, chat, jobs

    def select(self, chat, job):
        return self.client.post(f"/chats/{chat}/select_mockup", headers=self.manager,
                                data={"csrf": "csrf", "job_id": job}, follow_redirects=False)

    def page(self, chat):
        return self.client.get(f"/chats/{chat}", headers=self.manager).text

    def deliver(self, chat, response=None):
        self.resume(chat, "hook")
        return self.tick(response)

    def test_chosen_mockup_goes_to_mes_and_print(self):
        instance, _, chat, jobs = self.three_mockups()
        self.assertEqual(self.select(chat, jobs[1]).status_code, 303)
        record = self.record(instance)
        self.assertEqual(record["approved_image_job_id"], jobs[1])
        self.assertIn(f"макет №{jobs[1]}", json.loads(record["fields"])["approval_outcome"])
        calls = self.deliver(chat)
        self.assertEqual([call["print_file"]["filename"] for call in calls], ["m2.jpg"])
        self.assertEqual(self.action()["image_job_id"], jobs[1])
        with db.transaction() as con:
            job = con.execute("SELECT * FROM image_jobs WHERE id=?", (jobs[1],)).fetchone()
            self.assertEqual(job["placement_state"], "pending")
            self.assertTrue(placement.current(con, job))
            self.assertIsNone(con.execute("SELECT placement_state FROM image_jobs WHERE id=?", (jobs[0],)).fetchone()[0])

    def test_without_a_choice_the_last_sent_mockup_is_used_as_before(self):
        _, _, chat, jobs = self.three_mockups()
        calls = self.deliver(chat)
        self.assertEqual([call["print_file"]["filename"] for call in calls], ["p.jpg"])
        self.assertEqual(self.action()["image_job_id"], jobs[0])

    def test_changing_the_choice_before_delivery_uses_the_last_one(self):
        _, _, chat, jobs = self.three_mockups()
        self.select(chat, jobs[1])
        self.select(chat, jobs[2])
        self.assertEqual([call["print_file"]["filename"] for call in self.deliver(chat)], ["m3.jpg"])

    def test_nothing_is_sent_to_the_buyer_and_manual_mode_stays(self):
        instance, _, chat, jobs = self.three_mockups()
        with db.transaction() as con:
            before = con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        self.select(chat, jobs[2])
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], before)
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat,)).fetchone()[0], "manual")
            actions = [r[0] for r in con.execute("SELECT action FROM audit WHERE chat_id=?", (chat,))]
        self.assertIn("approval.manager_selected", actions)
        self.assertEqual(self.record(instance)["status"], "waiting_approval")

    def test_bot_waiting_for_an_answer_follows_the_approved_branch(self):
        instance, _, chat, jobs = self.three_mockups()
        with db.transaction() as con:
            con.execute("UPDATE chats SET mode='bot' WHERE id=?", (chat,))
        self.select(chat, jobs[2])
        self.assertEqual(self.record(instance)["node"], "hook")
        self.assertEqual([call["print_file"]["filename"] for call in self.tick()], ["m3.jpg"])

    def test_page_lists_mockups_and_marks_the_approved_one(self):
        _, _, chat, jobs = self.three_mockups()
        page = self.page(chat)
        self.assertIn("Какой макет идёт в печать", page)
        self.assertEqual(page.count("/select_mockup"), 3)
        self.assertIn("отправлен покупателю", page)
        self.assertIn("покупателю не отправлялся", page)
        self.select(chat, jobs[1])
        page = self.page(chat)
        self.assertEqual(page.count("/select_mockup"), 2)
        self.assertIn("Согласован, пойдёт в печать", page)
        self.assertIn('value="yes" data-submit-on-change checked', page)

    def test_single_mockup_needs_no_choice(self):
        _, _, chat, job = self.setup_mes()
        self.assertNotIn("Какой макет идёт в печать", self.page(chat))
        self.assertEqual(self.select(chat, job).status_code, 303)

    def test_only_a_finished_mockup_of_this_order_can_be_chosen(self):
        instance, item, chat, jobs = self.three_mockups()
        with db.transaction() as con:
            con.execute("UPDATE image_jobs SET state='failed' WHERE id=?", (jobs[2],))
            con.execute("UPDATE image_jobs SET print_file=NULL WHERE id=?", (jobs[1],))
        self.assertEqual(self.select(chat, jobs[2]).status_code, 422)
        self.assertEqual(self.select(chat, jobs[1]).status_code, 422)
        self.assertEqual(self.select(chat, 999999).status_code, 422)
        foreign_instance, foreign_item, foreign_chat, _ = self.setup_instance(posting="FOREIGN", external_chat="FOREIGN")
        with db.transaction() as con:
            mid = self.image(con, foreign_chat, foreign_item)
            foreign = con.execute(
                "INSERT INTO image_jobs(item_id,media_id,preview_media_id,prompt,width_cm,height_cm,state,print_file,"
                "created_at,updated_at) VALUES(?,?,?,'p',40,60,'completed','D:/x.jpg',?,?)",
                (foreign_item, mid, mid, db.now(), db.now())).lastrowid
        self.assertEqual(self.select(chat, foreign).status_code, 422)
        self.assertEqual(self.select(foreign_chat, jobs[0]).status_code, 403)
        self.assertIsNone(self.record(instance)["mockup_selected_job_id"])

    def test_cancelled_or_closed_order_refuses_the_choice(self):
        _, item, chat, jobs = self.three_mockups()
        with db.transaction() as con:
            con.execute("UPDATE items SET external_status='cancelled' WHERE id=?", (item,))
        self.assertEqual(self.select(chat, jobs[1]).status_code, 422)
        with db.transaction() as con:
            con.execute("UPDATE items SET external_status='awaiting_packaging' WHERE id=?", (item,))
            con.execute("UPDATE instances SET order_closed_at=? WHERE item_id=?", (db.now(), item))
        self.assertEqual(self.select(chat, jobs[1]).status_code, 422)

    def test_accepted_order_keeps_its_mockup(self):
        _, _, chat, jobs = self.three_mockups()
        self.select(chat, jobs[1])
        self.deliver(chat)
        with db.transaction() as con:
            con.execute("UPDATE instances SET order_closed_at=NULL")
        refused = self.select(chat, jobs[2])
        self.assertEqual(refused.status_code, 422)
        self.assertIn("MES уже приняла", refused.text)
        self.assertEqual(self.action()["image_job_id"], jobs[1])
        page = self.page(chat)
        self.assertIn("Передан в MES", page)
        self.assertNotIn("/select_mockup", page)

    def test_delivery_that_may_have_reached_mes_locks_the_choice(self):
        _, _, chat, jobs = self.three_mockups()
        self.deliver(chat, httpx.Response(503, json={"message": "later"}))
        self.assertEqual(self.action()["state"], "pending")
        self.assertEqual(self.action()["attempts"], 1)
        refused = self.select(chat, jobs[1])
        self.assertEqual(refused.status_code, 422)
        self.assertIn("могла его принять", refused.text)
        self.assertEqual(self.action()["image_job_id"], jobs[0])
        self.assertTrue(self.action()["payload"])

    def test_conflict_stays_for_manual_reconciliation(self):
        _, _, chat, jobs = self.three_mockups()
        self.deliver(chat, httpx.Response(409, json={"message": "conflict"}))
        self.assertEqual(self.action()["state"], "failed")
        self.assertEqual(self.select(chat, jobs[1]).status_code, 422)

    def test_refused_data_can_go_again_with_another_mockup_under_the_same_event(self):
        instance, _, chat, jobs = self.three_mockups()
        self.deliver(chat, httpx.Response(422, json={"message": "bad"}))
        event = self.action()
        self.assertEqual(event["state"], "failed")
        with db.transaction() as con:
            con.execute("UPDATE chats SET mode='manual' WHERE id=?", (chat,))
            con.execute("UPDATE outbox SET state='sent' WHERE state='pending'")
        self.assertEqual(self.select(chat, jobs[2]).status_code, 303)
        self.assertIsNone(self.action()["payload"])
        calls = self.deliver(chat)
        self.assertEqual([call["print_file"]["filename"] for call in calls], ["m3.jpg"])
        self.assertEqual(self.action()["id"], event["id"])
        self.assertEqual(self.action()["state"], "sent")

    def test_prepared_but_unsent_event_takes_the_new_choice(self):
        instance, _, chat, jobs = self.three_mockups()
        with db.transaction() as con:
            con.execute("INSERT INTO order_webhook_actions(id,instance_id,node_id,image_job_id,payload,due_at,created_at,protocol) "
                        "VALUES('e1',?,'hook',?,'{\"old\":true}',?,?,2)", (instance, jobs[0], db.now(), db.now()))
        self.assertEqual(self.select(chat, jobs[1]).status_code, 303)
        event = self.action()
        self.assertEqual((event["image_job_id"], event["payload"], event["manager_forced"]), (jobs[1], None, 1))

    def test_another_preview_sent_later_cancels_the_choice(self):
        instance, _, chat, jobs = self.three_mockups()
        self.select(chat, jobs[1])
        with db.transaction() as con:
            preview = con.execute("SELECT preview_media_id FROM image_jobs WHERE id=?", (jobs[2],)).fetchone()[0]
            self.sent_file(con, chat, preview, "newer")
            row = con.execute("SELECT * FROM instances WHERE id=?", (instance,)).fetchone()
            self.assertIsNone(scenarios.selected_mockup(con, row))
            self.assertFalse(scenarios.manager_approved(con, row))
        self.assertNotIn("Согласован, пойдёт в печать", self.page(chat))

    def test_sending_the_chosen_mockup_keeps_the_choice(self):
        instance, _, chat, jobs = self.three_mockups()
        self.select(chat, jobs[1])
        with db.transaction() as con:
            preview = con.execute("SELECT preview_media_id FROM image_jobs WHERE id=?", (jobs[1],)).fetchone()[0]
            self.sent_file(con, chat, preview, "same")
            row = con.execute("SELECT * FROM instances WHERE id=?", (instance,)).fetchone()
            self.assertEqual(scenarios.selected_mockup(con, row), jobs[1])

    def test_choice_works_when_no_preview_was_ever_sent(self):
        instance, item, chat, job = self.setup_mes(sent_preview=False)
        with db.transaction() as con:
            mid = self.image(con, chat, item)
            newer = con.execute(
                "INSERT INTO image_jobs(item_id,media_id,preview_media_id,prompt,width_cm,height_cm,state,print_file,"
                "owner_id,created_at,updated_at) VALUES(?,?,?,'p',40,60,'completed','D:/Print/new.jpg','owner',?,?)",
                (item, mid, mid, db.now(), db.now())).lastrowid
        self.assertEqual(self.select(chat, job).status_code, 303)
        self.assertEqual(self.deliver(chat)[0]["print_file"]["filename"], "p.jpg")
        self.assertNotEqual(job, newer)

    def test_checkbox_and_choice_replace_each_other(self):
        instance, _, chat, jobs = self.three_mockups()
        self.select(chat, jobs[1])
        self.check(chat, approved=False)
        record = self.record(instance)
        self.assertIsNone(record["mockup_approved_at"])
        self.assertIsNone(record["approved_image_job_id"])
        self.assertNotIn("Согласован, пойдёт в печать", self.page(chat))
        self.check(chat)
        record = self.record(instance)
        self.assertIsNone(record["mockup_selected_job_id"])
        self.assertEqual(self.deliver(chat)[0]["print_file"]["filename"], "p.jpg")


if __name__ == "__main__":
    unittest.main()
