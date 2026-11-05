import json
import unittest

import httpx

from app import db, placement, scenarios, worker
from tests import test_folio
from tests import test_manager_controls


class OrderCloseTest(unittest.TestCase):
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

    def graph(self, instance, change):
        with db.transaction() as con:
            row = con.execute("SELECT v.id,v.graph FROM versions v JOIN instances i ON i.version_id=v.id WHERE i.id=?",
                              (instance,)).fetchone()
            graph = json.loads(row["graph"])
            change({node["id"]: node for node in graph["nodes"]}, graph["nodes"])
            con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump(graph), row["id"]))

    def closed_order(self):
        instance, item, chat, job = self.setup_mes()
        self.resume(chat, "hook")
        self.tick()
        self.assertTrue(self.record(instance)["order_closed_at"])
        return instance, item, chat, job

    def buyer(self, chat, message="m-late", text="Спасибо"):
        with db.transaction() as con:
            row = con.execute("SELECT * FROM chats WHERE id=?", (chat,)).fetchone()
            worker.import_message(con, row, {"message_id": message, "user": {"type": "Customer"}, "data": [text]},
                                  None, False)
            event = con.execute("SELECT * FROM events WHERE state='pending' ORDER BY id DESC").fetchone()
            worker.process_event(con, event)

    def mode(self, chat):
        with db.transaction() as con:
            return con.execute("SELECT mode FROM chats WHERE id=?", (chat,)).fetchone()[0]

    def read(self, chat):
        self.client.post(f"/chats/{chat}/read", headers=self.manager, data={"csrf": "csrf"})

    def listed(self, chat, query=""):
        # The list shows only chats Folio started itself.
        with db.transaction() as con:
            job = con.execute("INSERT INTO jobs(kind,account_id,payload,state,created_at) VALUES('start_chat',1,'{}','done',?)",
                              (db.now(),)).lastrowid
            con.execute("UPDATE chats SET start_job_id=COALESCE(start_job_id,?),chat_type='BUYER_SELLER' WHERE id=?", (job, chat))
        return f'/chats/{chat}"' in self.client.get(f"/{query}", headers=self.manager).text

    def close(self, chat):
        return self.client.post(f"/chats/{chat}/close_order", headers=self.manager, data={"csrf": "csrf"},
                                follow_redirects=False)

    def test_mes_acceptance_closes_the_order(self):
        instance, _, chat, _ = self.closed_order()
        self.assertEqual(self.record(instance)["status"], "closed")
        page = self.client.get(f"/chats/{chat}", headers=self.manager).text
        self.assertIn("Заказ закрыт", page)
        self.assertNotIn("Бот работает", page)
        self.assertNotIn("Взять в работу", page)
        self.assertNotIn("/mockup_approval", page)
        self.assertIn("Заказ закрыт", self.client.get("/orders", headers=self.manager).text)
        with db.transaction() as con:
            actions = [r[0] for r in con.execute("SELECT action FROM audit WHERE chat_id=?", (chat,))]
        self.assertEqual(actions.count("order.closed"), 1)

    def test_refused_delivery_does_not_close(self):
        instance, _, chat, _ = self.setup_mes()
        self.resume(chat, "hook")
        self.tick(httpx.Response(422, json={"message": "x"}))
        self.assertEqual(self.action()["state"], "failed")
        self.assertIsNone(self.record(instance)["order_closed_at"])

    def test_delivery_to_be_retried_does_not_close(self):
        instance, _, chat, _ = self.setup_mes()
        self.resume(chat, "hook")
        self.tick(httpx.Response(503, json={"message": "x"}))
        self.assertEqual(self.action()["state"], "pending")
        self.assertIsNone(self.record(instance)["order_closed_at"])

    def test_scenario_end_without_mes_is_not_an_order_closure(self):
        instance, _, chat, _ = self.setup_mes()
        self.resume(chat, "done")
        record = self.record(instance)
        self.assertEqual(record["status"], "closed")
        self.assertIsNone(record["order_closed_at"])
        self.assertTrue(self.listed(chat))
        self.assertNotIn("Заказ закрыт", self.client.get(f"/chats/{chat}", headers=self.manager).text)

    def test_steps_after_mes_run_before_closing(self):
        instance, _, chat, _ = self.setup_mes()

        def extend(index, nodes):
            index["hook"]["next"] = "pause"
            nodes.append({"id": "pause", "kind": "wait", "text": "Ждём ответ", "next": "thanks"})
            nodes.append({"id": "thanks", "kind": "send", "text": "Спасибо за заказ", "next": "done"})

        self.graph(instance, extend)
        self.resume(chat, "hook")
        self.tick()
        self.assertEqual(self.record(instance)["node"], "pause")
        self.assertIsNone(self.record(instance)["order_closed_at"])
        with db.transaction() as con:
            con.execute("UPDATE outbox SET state='sent' WHERE chat_id=?", (chat,))
        self.buyer(chat, text="Хорошо")
        self.assertTrue(self.record(instance)["order_closed_at"])
        # The last message of the scenario still goes out after closure.
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT state FROM outbox WHERE body='Спасибо за заказ'").fetchone()[0], "pending")

    def test_mes_followed_by_brief_check_closes_too(self):
        instance, _, chat, _ = self.setup_mes()
        self.graph(instance, lambda index, _: index["hook"].update(next="review"))
        self.resume(chat, "hook")
        self.tick()
        record = self.record(instance)
        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["order_closed_at"])

    def test_acceptance_in_manual_mode_closes_at_once(self):
        instance, _, chat, _ = self.setup_mes()
        self.resume(chat, "hook")
        with db.transaction() as con:
            scenarios.takeover(con, chat, "2")
        self.tick()
        record = self.record(instance)
        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["order_closed_at"])
        self.assertEqual(self.mode(chat), "manual")

    def test_closed_chat_leaves_active_list_but_stays_reachable(self):
        _, _, chat, _ = self.closed_order()
        self.read(chat)
        self.assertFalse(self.listed(chat))
        self.assertFalse(self.listed(chat, "?mode=bot"))
        self.assertTrue(self.listed(chat, "?closed=true"))
        self.assertTrue(self.listed(chat, "?q=ORDER"))
        self.assertEqual(self.client.get(f"/chats/{chat}", headers=self.manager).status_code, 200)

    def test_active_chat_is_not_in_closed_filter(self):
        _, _, chat, _ = self.setup_mes()
        self.assertTrue(self.listed(chat))
        self.assertFalse(self.listed(chat, "?closed=true"))

    def test_buyer_message_returns_the_chat_to_a_manager(self):
        instance, _, chat, _ = self.closed_order()
        self.read(chat)
        self.assertEqual(self.mode(chat), "bot")
        self.buyer(chat)
        record = self.record(instance)
        self.assertIsNone(record["order_closed_at"])
        self.assertEqual(record["status"], "needs_manager")
        self.assertEqual(self.mode(chat), "manual")
        self.assertTrue(self.listed(chat))
        self.assertTrue(self.listed(chat, "?mode=manual"))
        page = self.client.get(f"/chats/{chat}", headers=self.manager).text
        self.assertIn("Закрыть заказ", page)
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM outbox WHERE state='pending'").fetchone()[0], 0)

    def test_manager_closes_a_reopened_order(self):
        instance, _, chat, _ = self.closed_order()
        self.buyer(chat)
        self.assertEqual(self.close(chat).status_code, 303)
        record = self.record(instance)
        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["order_closed_at"])
        self.read(chat)
        self.assertFalse(self.listed(chat))
        self.assertEqual(self.close(chat).status_code, 303)

    def test_closure_requires_mes_and_a_live_order_and_access(self):
        instance, item, chat, _ = self.setup_mes()
        self.assertNotIn("/close_order", self.client.get(f"/chats/{chat}", headers=self.manager).text)
        refused = self.close(chat)
        self.assertEqual(refused.status_code, 422)
        self.assertIn("принят MES", refused.text)
        self.assertIsNone(self.record(instance)["order_closed_at"])
        self.resume(chat, "hook")
        self.tick()
        self.buyer(chat)
        foreign = self.setup_instance(posting="FOREIGN", external_chat="FOREIGN")[2]
        self.assertEqual(self.close(foreign).status_code, 403)
        with db.transaction() as con:
            con.execute("UPDATE items SET external_status='cancelled' WHERE id=?", (item,))
        self.assertEqual(self.close(chat).status_code, 422)

    def test_cancelled_order_stays_cancelled(self):
        _, item, chat, _ = self.closed_order()
        with db.transaction() as con:
            con.execute("UPDATE items SET external_status='cancelled' WHERE id=?", (item,))
        self.assertIn("Заказ отменён", self.client.get(f"/chats/{chat}", headers=self.manager).text)

    def test_resume_reopens_and_mes_is_not_sent_twice(self):
        instance, _, chat, _ = self.closed_order()
        self.assertEqual(self.resume(chat, "approve").status_code, 303)
        self.assertIsNone(self.record(instance)["order_closed_at"])
        self.assertTrue(self.listed(chat))
        with db.transaction() as con:
            con.execute("UPDATE outbox SET state='sent' WHERE state='pending'")
        self.assertEqual(self.resume(chat, "hook").status_code, 303)
        self.assertEqual(self.tick(), [])
        self.assertTrue(self.record(instance)["order_closed_at"])

    def test_placement_goes_on_after_closure(self):
        _, _, _, job = self.closed_order()
        with db.transaction() as con:
            row = con.execute("SELECT * FROM image_jobs WHERE id=?", (job,)).fetchone()
            self.assertEqual(row["placement_state"], "pending")
            self.assertTrue(placement.current(con, row))

    def test_closed_order_keeps_its_approval(self):
        instance, _, chat, _ = self.closed_order()
        response = self.check(chat, approved=False)
        self.assertEqual(response.status_code, 422)
        self.assertIn("Заказ закрыт", response.text)
        with db.transaction() as con:
            row = con.execute("SELECT * FROM image_jobs").fetchone()
            self.assertTrue(placement.current(con, row))
        self.assertTrue(self.record(instance)["order_closed_at"])


if __name__ == "__main__":
    unittest.main()
