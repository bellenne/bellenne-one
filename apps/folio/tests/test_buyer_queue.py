import io
import json
import unittest
from unittest.mock import patch

from PIL import Image

from app import db, scenarios, worker
from tests import test_folio


class BuyerQueueTest(unittest.TestCase):
    setUp = test_folio.FolioTest.setUp
    tearDown = test_folio.FolioTest.tearDown
    setup_instance = test_folio.FolioTest.setup_instance

    def two_orders(self):
        """Two orders in one Ozon chat: the second was started while the first is open."""
        first = self.setup_instance(posting="A", external_chat="SHARED")
        second = self.setup_instance(posting="B", external_chat="SHARED")
        with db.transaction() as con:
            con.execute("INSERT INTO assignments VALUES(?,'2')", (first[2],))
            con.execute("INSERT INTO assignments VALUES(?,'2')", (second[2],))
            con.execute("DELETE FROM outbox WHERE chat_id=?", (second[2],))
            con.execute("UPDATE instances SET status='collecting',revision=0,prompted=0,node="
                        "(SELECT node FROM instances WHERE id=?) WHERE id=?", (second[0], second[0]))
            scenarios.begin_or_queue(con, second[0])
        return first, second

    def sync(self, *messages):
        class Adapter:
            client = None
            def seller_info(self): return {}
            def order_pages(self, *_): return iter([])
            def chats(self): return iter([{"chat": {"chat_id": "SHARED", "chat_type": "BUYER_SELLER"}}])
            def history(self, _): return iter(messages)
        settings = {**self.settings, "sync_since": "2026-09-23T00:00:00+00:00"}
        for _ in range(2):
            worker.sync_job({"kind": "sync", "account_id": 1, "payload": "{}"}, Adapter(), settings)

    def buyer(self, message="m1", text="Вопрос", **extra):
        return {"message_id": message, "created_at": db.now(), "user": {"type": "Customer"}, "data": [text], **extra}

    def value(self, sql, *args):
        with db.transaction() as con:
            row = con.execute(sql, args).fetchone()
            return row[0] if row else None

    def status(self, instance):
        return self.value("SELECT status FROM instances WHERE id=?", instance)

    def messages(self, chat):
        with db.transaction() as con:
            return [r[0] for r in con.execute("SELECT external_id FROM messages WHERE chat_id=? AND actor='buyer'", (chat,))]

    def release(self):
        with db.transaction() as con:
            scenarios.release_queue(con)

    def test_second_order_waits_silently(self):
        _, second = self.two_orders()
        self.assertEqual(self.status(second[0]), "waiting_queue")
        self.assertEqual(self.value("SELECT COUNT(*) FROM outbox WHERE chat_id=?", second[2]), 0)
        self.release()
        self.assertEqual(self.status(second[0]), "waiting_queue")

    def test_first_order_of_a_buyer_starts_at_once(self):
        instance, _, chat, _ = self.setup_instance()
        with db.transaction() as con:
            con.execute("DELETE FROM outbox")
            con.execute("UPDATE instances SET revision=0,prompted=0 WHERE id=?", (instance,))
            scenarios.begin_or_queue(con, instance)
        self.assertEqual(self.status(instance), "collecting")
        self.assertEqual(self.value("SELECT COUNT(*) FROM outbox WHERE chat_id=?", chat), 1)

    def test_other_buyers_order_does_not_block(self):
        self.setup_instance(posting="OTHER", external_chat="OTHER-CHAT")
        instance, _, _, _ = self.setup_instance(posting="MINE", external_chat="MY-CHAT")
        self.assertEqual(self.status(instance), "collecting")
        with db.transaction() as con:
            self.assertIsNone(scenarios.queue_ahead(con, con.execute("SELECT chat_id FROM instances WHERE id=?", (instance,)).fetchone()[0]))

    def test_message_goes_only_to_the_current_order_and_bot_keeps_working(self):
        first, second = self.two_orders()
        self.sync(self.buyer())
        self.assertEqual(self.messages(first[2]), ["m1"])
        self.assertEqual(self.messages(second[2]), [])
        self.assertEqual(self.value("SELECT mode FROM chats WHERE id=?", first[2]), "bot")
        self.assertEqual(self.value("SELECT chat_id FROM events"), first[2])
        self.assertEqual(self.value("SELECT unread FROM chats WHERE id=?", second[2]), 0)

    def test_photo_is_kept_and_reaches_the_current_order(self):
        first, second = self.two_orders()
        out = io.BytesIO()
        Image.new("RGB", (10, 10)).save(out, "PNG")
        with patch("app.media.download", return_value=out.getvalue()) as download:
            self.sync(self.buyer("p1", "![photo](https://cdn.ozon.ru/photo.png)"))
        download.assert_called_once()
        media_ids = json.loads(self.value("SELECT media_ids FROM messages WHERE external_id='p1'"))
        self.assertEqual(len(media_ids), 1)
        self.assertEqual(self.value("SELECT chat_id FROM media WHERE id=?", media_ids[0]), first[2])
        self.assertEqual(self.value("SELECT COUNT(*) FROM messages WHERE external_id='p1'"), 1)

    def test_explicit_order_number_still_wins(self):
        first, second = self.two_orders()
        with db.transaction() as con:
            con.execute("UPDATE items SET order_number=posting")
        self.sync(self.buyer("m-b", "Да", context={"order_number": "B"}))
        self.assertEqual(self.messages(second[2]), ["m-b"])
        self.assertEqual(self.messages(first[2]), [])
        self.assertEqual(self.status(second[0]), "waiting_queue")

    def test_next_order_starts_when_the_current_one_finishes(self):
        for name, finish in {
            "closed by MES": lambda con, i: con.execute("UPDATE instances SET status='closed',order_closed_at=? WHERE id=?", (db.now(), i[0])),
            "scenario ended": lambda con, i: con.execute("UPDATE instances SET status='closed' WHERE id=?", (i[0],)),
            "brief ready": lambda con, i: con.execute("UPDATE instances SET status='ready' WHERE id=?", (i[0],)),
            "cancelled": lambda con, i: con.execute("UPDATE items SET external_status='cancelled' WHERE id=?", (i[1],)),
            "delivered": lambda con, i: con.execute("UPDATE items SET external_status='delivered' WHERE id=?", (i[1],)),
        }.items():
            with self.subTest(name):
                first, second = self.two_orders()
                with db.transaction() as con:
                    finish(con, first)
                self.release()
                self.assertEqual(self.status(second[0]), "collecting")
                self.assertEqual(self.value("SELECT COUNT(*) FROM outbox WHERE chat_id=? AND state='pending'", second[2]), 1)
                self.tearDown()
                self.setUp()

    def test_stuck_order_keeps_holding_the_next(self):
        first, second = self.two_orders()
        with db.transaction() as con:
            con.execute("UPDATE instances SET status='needs_manager' WHERE id=?", (first[0],))
        self.release()
        self.assertEqual(self.status(second[0]), "waiting_queue")

    def test_queue_goes_one_order_at_a_time(self):
        first, second = self.two_orders()
        third = self.setup_instance(posting="C", external_chat="SHARED")
        with db.transaction() as con:
            con.execute("UPDATE instances SET status='waiting_queue' WHERE id=?", (third[0],))
            con.execute("DELETE FROM outbox WHERE chat_id=?", (third[2],))
            con.execute("UPDATE instances SET status='closed',order_closed_at=? WHERE id=?", (db.now(), first[0]))
        self.release()
        self.assertEqual(self.status(second[0]), "collecting")
        self.assertEqual(self.status(third[0]), "waiting_queue")
        self.assertEqual(self.value("SELECT COUNT(*) FROM outbox WHERE chat_id=?", third[2]), 0)

    def test_closed_order_does_not_take_a_message_from_the_next(self):
        first, second = self.two_orders()
        with db.transaction() as con:
            con.execute("UPDATE instances SET status='closed',order_closed_at=? WHERE id=?", (db.now(), first[0]))
        self.release()
        with db.transaction() as con:
            con.execute("UPDATE outbox SET state='sent'")
        self.sync(self.buyer("m2"))
        self.assertEqual(self.messages(second[2]), ["m2"])
        self.assertEqual(self.messages(first[2]), [])
        self.assertTrue(self.value("SELECT order_closed_at FROM instances WHERE id=?", first[0]))

    def test_when_all_are_done_the_latest_order_returns_to_a_manager(self):
        first, second = self.two_orders()
        with db.transaction() as con:
            con.execute("UPDATE instances SET status='closed',order_closed_at=? WHERE id IN (?,?)", (db.now(), first[0], second[0]))
        self.sync(self.buyer("m3"))
        self.assertEqual(self.messages(second[2]), ["m3"])
        self.assertEqual(self.messages(first[2]), [])
        with db.transaction() as con:
            worker.process_event(con, con.execute("SELECT * FROM events").fetchone())
        self.assertEqual(self.status(second[0]), "needs_manager")
        self.assertTrue(self.value("SELECT order_closed_at FROM instances WHERE id=?", first[0]))

    def test_manager_starts_a_waiting_order(self):
        first, second = self.two_orders()
        page = self.client.get(f"/chats/{second[2]}", headers=self.manager).text
        self.assertIn("Заказ ждёт очереди", page)
        self.assertIn("Ждёт очереди", page)
        self.assertIn(f'/chats/{first[2]}">A</a>', page)
        self.assertIn(f'/chats/{second[2]}">B</a>', self.client.get(f"/chats/{first[2]}", headers=self.manager).text)
        response = self.client.post(f"/chats/{second[2]}/queue_start", headers=self.manager,
                                    data={"csrf": "csrf"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.status(second[0]), "collecting")
        self.assertEqual(self.value("SELECT COUNT(*) FROM outbox WHERE chat_id=? AND state='pending'", second[2]), 1)
        self.assertNotIn("Заказ ждёт очереди", self.client.get(f"/chats/{second[2]}", headers=self.manager).text)
        # Its replies now go there, the older order stays as it was.
        with db.transaction() as con:
            con.execute("UPDATE outbox SET state='sent'")
        self.sync(self.buyer("m4"))
        self.assertEqual(self.messages(second[2]), ["m4"])
        self.assertEqual(self.status(first[0]), "collecting")
        again = self.client.post(f"/chats/{second[2]}/queue_start", headers=self.manager, data={"csrf": "csrf"})
        self.assertEqual(again.status_code, 422)

    def test_start_is_refused_without_access_or_for_finished_order(self):
        _, second = self.two_orders()
        foreign = self.setup_instance(posting="FOREIGN", external_chat="FOREIGN")[2]
        self.assertEqual(self.client.post(f"/chats/{foreign}/queue_start", headers=self.manager,
                                          data={"csrf": "csrf"}).status_code, 403)
        with db.transaction() as con:
            con.execute("UPDATE items SET external_status='delivered' WHERE id=?", (second[1],))
        self.assertEqual(self.client.post(f"/chats/{second[2]}/queue_start", headers=self.manager,
                                          data={"csrf": "csrf"}).status_code, 422)

    def test_manager_writing_in_an_order_makes_it_current(self):
        first, second = self.two_orders()
        with db.transaction() as con:
            con.execute("UPDATE outbox SET state='sent'")
        response = self.client.post(f"/chats/{second[2]}/send", headers=self.manager,
                                    data={"csrf": "csrf", "text": "По второму заказу", "dedup": "d1"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.status(second[0]), "needs_manager")
        with db.transaction() as con:
            con.execute("UPDATE outbox SET state='sent'")
        self.sync(self.buyer("m5"))
        self.assertEqual(self.messages(second[2]), ["m5"])
        self.assertEqual(self.messages(first[2]), [])
        # The manager turning back to the first order moves the buyer's replies with them.
        self.client.post(f"/chats/{first[2]}/send", headers=self.manager,
                         data={"csrf": "csrf", "text": "А теперь по первому", "dedup": "d2"}, follow_redirects=False)
        with db.transaction() as con:
            con.execute("UPDATE outbox SET state='sent'")
        self.sync(self.buyer("m6"))
        self.assertEqual(self.messages(first[2]), ["m6"])

    def test_system_handoff_does_not_move_the_replies(self):
        first, second = self.two_orders()
        with db.transaction() as con:
            scenarios.takeover(con, second[2], "system")
            self.assertIsNone(con.execute("SELECT queue_priority_at FROM instances WHERE id=?", (second[0],)).fetchone()[0])
            self.assertEqual(con.execute("SELECT status FROM instances WHERE id=?", (second[0],)).fetchone()[0], "waiting_queue")

    def test_message_older_than_both_orders_is_not_imported(self):
        first, second = self.two_orders()
        self.sync(self.buyer("old", created_at="2020-01-01T00:00:00+00:00"))
        self.assertEqual(self.messages(first[2]), [])
        self.assertEqual(self.messages(second[2]), [])

    def test_seller_message_without_a_match_is_still_not_guessed(self):
        first, second = self.two_orders()
        self.sync({"message_id": "s1", "created_at": db.now(), "user": {"type": "Seller"}, "data": ["Из кабинета"]})
        self.assertEqual(self.value("SELECT COUNT(*) FROM messages WHERE external_id='s1'"), 0)

    def test_broken_scenario_goes_to_a_manager_and_does_not_stop_the_rest(self):
        first, second = self.two_orders()
        other = self.setup_instance(posting="OTHER", external_chat="OTHER-CHAT")
        with db.transaction() as con:
            con.execute("UPDATE instances SET status='closed',order_closed_at=? WHERE id=?", (db.now(), first[0]))
            con.execute("UPDATE instances SET status='waiting_queue' WHERE id=?", (other[0],))
        real = scenarios.advance

        def broken(con, instance_id, *args, **kwargs):
            if instance_id == second[0]:
                raise RuntimeError("broken graph")
            return real(con, instance_id, *args, **kwargs)

        with patch("app.scenarios.advance", side_effect=broken):
            self.release()
        self.assertEqual(self.status(second[0]), "needs_manager")
        self.assertEqual(self.value("SELECT mode FROM chats WHERE id=?", second[2]), "manual")
        self.assertEqual(self.status(other[0]), "collecting")


if __name__ == "__main__":
    unittest.main()
