import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from app import db, media, scenarios, security, worker
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
            "media_max_bytes": 1048576,
            "media_mimes": ["image/png"],
            "http_timeout": 1,
            "request_interval": 0.001,
            "message_max_chars": 1000,
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
            con.execute(
                "INSERT INTO mappings(account_id,offer_id,product_type,scenario_id,template_ids) VALUES(1,?,?,?,'[1]')",
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
                            "sku": str(sid),
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
            "/settings",
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
            self.client.post("/settings", headers=self.admin).status_code, 403
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


if __name__ == "__main__":
    unittest.main()
