import io
import itertools
import json
import socket
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from app import db, image_tasks, media, presentation, scenarios, wish_intent
from tests import test_folio

ORDERS = itertools.count(1)


def picture(width=20, height=20, *, kind="PNG", exif=None):
    out = io.BytesIO()
    options = {"exif": exif} if exif else {}
    Image.new("RGB", (width, height), "blue").save(out, kind, **options)
    return out.getvalue()


def resolver(table):
    def lookup(host, port, *_, **__):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in table[host]]
    return lookup


class DownloadTest(unittest.TestCase):
    def fetch(self, url, handler, table=None):
        table = table or {"img.example.com": ["93.184.216.34"]}
        with patch("app.media.socket.getaddrinfo", resolver(table)):
            return media.download_public(url, transport=httpx.MockTransport(handler))

    def test_request_goes_to_the_checked_address_under_the_original_name(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, headers={"content-type": "image/png"}, content=picture())

        self.assertEqual(self.fetch("https://img.example.com/a.png?x=1", handler), picture())
        self.assertEqual(seen[0].url.host, "93.184.216.34")
        self.assertEqual(seen[0].url.query, b"x=1")
        self.assertEqual(seen[0].headers["host"], "img.example.com")
        self.assertEqual(seen[0].extensions["sni_hostname"], "img.example.com")

    def test_links_to_internal_addresses_are_refused(self):
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"):
            with self.subTest(address), self.assertRaisesRegex(scenarios.Invalid, "внутренний адрес"):
                self.fetch("https://evil.example.com/a.png", lambda _: self.fail("request was sent"),
                           {"evil.example.com": [address]})

    def test_one_internal_address_among_public_ones_is_enough_to_refuse(self):
        with self.assertRaisesRegex(scenarios.Invalid, "внутренний адрес"):
            self.fetch("https://evil.example.com/a.png", lambda _: self.fail("request was sent"),
                       {"evil.example.com": ["93.184.216.34", "127.0.0.1"]})

    def test_redirect_is_checked_again_before_following(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"location": "http://metadata.internal/latest"})

        with self.assertRaisesRegex(scenarios.Invalid, "внутренний адрес"):
            self.fetch("https://img.example.com/a.png", handler,
                       {"img.example.com": ["93.184.216.34"], "metadata.internal": ["169.254.169.254"]})
        self.assertEqual(len(calls), 1)

    def test_relative_redirect_and_redirect_limit(self):
        def relative(request):
            if request.url.path == "/a":
                return httpx.Response(301, headers={"location": "/b.png"})
            return httpx.Response(200, content=picture())

        self.assertEqual(self.fetch("https://img.example.com/a", relative), picture())
        with self.assertRaisesRegex(scenarios.Invalid, "Слишком много перенаправлений"):
            self.fetch("https://img.example.com/a",
                       lambda _: httpx.Response(302, headers={"location": "/a"}))

    def test_unusable_links_are_refused_without_a_request(self):
        for url in ("ftp://img.example.com/a.png", "file:///etc/passwd", "img.example.com/a.png", "javascript:alert(1)",
                    "https://user:secret@img.example.com/a.png", "https://img.example.com:8080/a.png",
                    "https://img.example.com/a b.png", "https://" + "a" * 2050):
            with self.subTest(url), self.assertRaisesRegex(scenarios.Invalid, "Проверьте ссылку"):
                self.fetch(url, lambda _: self.fail("request was sent"))

    def test_clear_errors_for_what_a_site_returns(self):
        cases = (
            (lambda: httpx.Response(404), "код 404"),
            (lambda: httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=b"<html>"), "страница"),
            (lambda: httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"not an image"), "неподдерживаемое"),
            (lambda: httpx.Response(200, content=b"x" * (10 * 1024 * 1024 + 1)), "больше 10 МБ"),
            (lambda: httpx.Response(200, content=picture(kind="GIF")), "не разрешён"),
        )
        for response, message in cases:
            with self.subTest(message), self.assertRaisesRegex(scenarios.Invalid, message):
                self.fetch("https://img.example.com/a.png", lambda _, make=response: make())

    def test_unreachable_address_falls_back_to_the_next_and_then_explains(self):
        def handler(request):
            if ":" in request.url.host:
                raise httpx.ConnectError("no route")
            return httpx.Response(200, content=picture())

        table = {"img.example.com": ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"]}
        self.assertEqual(self.fetch("https://img.example.com/a.png", handler, table), picture())

        def down(_):
            raise httpx.ConnectError("no route")

        with self.assertRaisesRegex(scenarios.Invalid, "Не удалось связаться"):
            self.fetch("https://img.example.com/a.png", down)

    def test_unknown_site(self):
        with patch("app.media.socket.getaddrinfo", side_effect=socket.gaierror), \
             self.assertRaisesRegex(scenarios.Invalid, "не найден"):
            media.download_public("https://nope.example.com/a.png", transport=httpx.MockTransport(lambda _: self.fail()))


class LinkPhotoTest(unittest.TestCase):
    setUp = test_folio.FolioTest.setUp
    tearDown = test_folio.FolioTest.tearDown
    setup_instance = test_folio.FolioTest.setup_instance

    def setup_chat(self, *, manual=True, kind="portrait_background", limit=None):
        instance, item, chat, _ = self.setup_instance(kind)
        with db.transaction() as con:
            con.execute("INSERT INTO assignments VALUES(?,'2')", (chat,))
            if limit:
                row = con.execute("SELECT v.id,v.graph FROM versions v JOIN instances i ON i.version_id=v.id WHERE i.id=?",
                                  (instance,)).fetchone()
                graph = json.loads(row["graph"])
                for node in graph["nodes"]:
                    if node["kind"] in {"ask_photo", "ask_input"}:
                        node["max"] = limit
                con.execute("UPDATE versions SET graph=? WHERE id=?", (db.dump(graph), row["id"]))
            if manual:
                scenarios.takeover(con, chat, "2")
        return instance, item, chat

    def add(self, chat, content=None, *, url="https://img.example.com/a.png", **extra):
        target = {"side_effect": content} if isinstance(content, Exception) else {"return_value": content or picture()}
        with patch("app.media.download_public", **target) as download:
            response = self.client.post(f"/chats/{chat}/photo_url", headers=self.manager,
                                        data={"csrf": "csrf", "url": url, **extra}, follow_redirects=False)
        response.download = download
        return response

    def fields(self, instance):
        with db.transaction() as con:
            return json.loads(con.execute("SELECT fields FROM instances WHERE id=?", (instance,)).fetchone()[0])

    def test_link_photo_enters_the_brief_and_stays_in_manual_mode(self):
        instance, item, chat = self.setup_chat()
        with db.transaction() as con:
            before = con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        response = self.add(chat)
        self.assertEqual(response.status_code, 303)
        response.download.assert_called_once_with("https://img.example.com/a.png")
        photos = self.fields(instance)["photos"]
        self.assertEqual(len(photos), 1)
        with db.transaction() as con:
            stored = con.execute("SELECT * FROM media WHERE id=?", (photos[0],)).fetchone()
            self.assertEqual((stored["chat_id"], stored["item_id"], stored["mime"]), (chat, item, "image/png"))
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat,)).fetchone()[0], "manual")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], before)
            message = con.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC", (chat,)).fetchone()
            self.assertNotIn("img.example.com", message["body"])
            self.assertEqual(json.loads(message["media_ids"]), photos)
            self.assertEqual({s["id"] for s in image_tasks.rework_sources(con, chat, item)}, set(photos))
            actions = [r[0] for r in con.execute("SELECT action FROM audit WHERE chat_id=?", (chat,))]
        self.assertIn("media.added_by_link", actions)
        # The note stays inside Folio, so it must not claim the buyer received it.
        self.assertNotIn("Отправлено</span>", self.client.get(f"/chats/{chat}", headers=self.manager).text)
        self.assertTrue((db.DATA / "media" / photos[0]).is_file())

    def test_download_runs_without_a_database_lock(self):
        _, _, chat = self.setup_chat()

        def download(_url):
            # Would stall and fail with "database is locked" under an open transaction.
            with db.transaction() as con:
                con.execute("UPDATE chats SET unread=unread WHERE id=?", (chat,))
            return picture()

        with patch("app.media.download_public", side_effect=download):
            response = self.client.post(f"/chats/{chat}/photo_url", headers=self.manager,
                                        data={"csrf": "csrf", "url": "https://img.example.com/a.png"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def test_same_file_twice_is_one_photo(self):
        instance, _, chat = self.setup_chat(limit=3)
        self.add(chat)
        self.add(chat)
        self.assertEqual(len(self.fields(instance)["photos"]), 1)
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM messages WHERE body LIKE '%по ссылке'").fetchone()[0], 1)

    def test_single_photo_brief_takes_the_new_photo(self):
        instance, _, chat = self.setup_chat(limit=1)
        self.add(chat, picture(10, 10))
        first = self.fields(instance)["photos"]
        self.add(chat, picture(30, 30))
        second = self.fields(instance)["photos"]
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first, second)

    def test_several_photo_brief_keeps_its_limit(self):
        instance, _, chat = self.setup_chat(limit=2)
        self.add(chat, picture(10, 10))
        self.add(chat, picture(20, 20))
        response = self.add(chat, picture(30, 30))
        self.assertEqual(response.status_code, 422)
        self.assertIn("максимум фото", response.text)
        response.download.assert_not_called()
        self.assertEqual(len(self.fields(instance)["photos"]), 2)

    def test_bot_waiting_for_a_photo_continues(self):
        instance, _, chat = self.setup_chat(manual=False, limit=1)
        with db.transaction() as con:
            before = con.execute("SELECT node FROM instances WHERE id=?", (instance,)).fetchone()[0]
        self.assertEqual(self.add(chat).status_code, 303)
        with db.transaction() as con:
            after = con.execute("SELECT node FROM instances WHERE id=?", (instance,)).fetchone()[0]
            self.assertEqual(con.execute("SELECT mode FROM chats WHERE id=?", (chat,)).fetchone()[0], "bot")
        self.assertNotEqual(before, after)
        self.assertEqual(len(self.fields(instance)["photos"]), 1)

    def test_refusals_change_nothing(self):
        instance, item, chat = self.setup_chat()
        failed = self.add(chat, scenarios.Invalid("По ссылке страница, а не изображение"))
        self.assertEqual(failed.status_code, 422)
        self.assertIn("страница", failed.text)
        self.assertEqual(self.fields(instance).get("photos", []), [])
        self.assertEqual(self.add(chat, url=" ").status_code, 422)
        self.assertEqual(self.client.post(f"/chats/{chat}/photo_url", headers=self.manager,
                                          data={"csrf": "wrong", "url": "https://a.example.com/a"}).status_code, 403)
        foreign = self.setup_instance(posting="FOREIGN", external_chat="FOREIGN")[2]
        denied = self.add(foreign)
        self.assertEqual(denied.status_code, 403)
        denied.download.assert_not_called()
        with db.transaction() as con:
            con.execute("UPDATE items SET external_status='cancelled' WHERE id=?", (item,))
        cancelled = self.add(chat)
        self.assertEqual(cancelled.status_code, 422)
        cancelled.download.assert_not_called()

    def test_order_cancelled_during_download_stores_nothing(self):
        _, item, chat = self.setup_chat()

        def download(_url):
            with db.transaction() as con:
                con.execute("UPDATE items SET external_status='cancelled' WHERE id=?", (item,))
            return picture()

        with patch("app.media.download_public", side_effect=download):
            response = self.client.post(f"/chats/{chat}/photo_url", headers=self.manager,
                                        data={"csrf": "csrf", "url": "https://img.example.com/a.png"})
        self.assertEqual(response.status_code, 422)
        with db.transaction() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM media").fetchone()[0], 0)

    def test_chat_offers_links_from_buyer_messages_only(self):
        _, _, chat = self.setup_chat()
        with db.transaction() as con:
            con.execute("INSERT INTO messages(chat_id,actor,body,media_ids,created_at) VALUES(?,?,?,?,?)",
                        (chat, "buyer", "Вот фото https://img.example.com/buyer.jpg, спасибо", "[]", db.now()))
            con.execute("INSERT INTO messages(chat_id,actor,body,media_ids,created_at) VALUES(?,?,?,?,?)",
                        (chat, "manager:2", "Смотрите https://img.example.com/manager.jpg", "[]", db.now()))
        page = self.client.get(f"/chats/{chat}", headers=self.manager).text
        self.assertIn('value="https://img.example.com/buyer.jpg"', page)
        self.assertNotIn('value="https://img.example.com/manager.jpg"', page)
        self.assertIn("Добавить как фото для макета", page)
        self.assertIn("Добавить фото по ссылке", page)

    def test_message_links(self):
        self.assertEqual(presentation.message_links("Смотри (https://a.ru/1.jpg), http://b.ru/2.png! Ещё https://a.ru/1.jpg."),
                         ["https://a.ru/1.jpg", "http://b.ru/2.png"])
        self.assertEqual(presentation.message_links("без ссылок"), [])
        self.assertEqual(presentation.message_links(None), [])


class GenerationDecisionTest(unittest.TestCase):
    setUp = test_folio.FolioTest.setUp
    tearDown = test_folio.FolioTest.tearDown
    setup_instance = test_folio.FolioTest.setup_instance

    def test_print_size_rule(self):
        # 40×60 cm needs 2362×3543 px, 30×40 cm 1772×2362, 100×150 cm 5906×8858.
        for width, height, canvas, expected in (
            (2000, 4000, (40, 60), True),     # 362 and 543 px short after the crop
            (900, 1100, (40, 60), False),     # 1629 and 2443 px short
            (2362, 3543, (40, 60), True),     # exactly the print size
            (5000, 7000, (40, 60), True),     # larger than needed
            (1500, 2400, (40, 60), True),     # 862 and 1293 px short
            (1200, 2100, (40, 60), False),    # 1743 px of height are missing
            (4000, 2000, (40, 60), False),    # turned the other way
            (6000, 3000, (40, 60), False),    # large, but the crop would lose most of the frame
            (3000, 3000, (40, 60), True),     # a square fits either way
            (3000, 2000, (60, 40), True),     # landscape canvas
            (2000, 3000, (100, 150), False),  # a large canvas needs far more pixels
            (900, 1100, (30, 40), True),      # a small canvas needs far fewer
        ):
            with self.subTest(f"{width}x{height} on {canvas}"):
                self.assertEqual(image_tasks.print_only(width, height, *canvas), expected)

    def test_only_known_refusals_count_as_no_changes(self):
        for text in ("Без изменений", "без изменений, спасибо!", "Оставьте как есть.", "нет", "Фон не менять",
                     "ничего менять не нужно", "Всё как есть", "Оставьте все как есть",
                     "Добрый день, оставьте всё как есть, спасибо", "Фон оставьте как есть", "Ничего не меняйте",
                     "Хотела бы посмотреть макет", "Без согласования", "Согласование не нужно"):
            with self.subTest(text):
                self.assertTrue(wish_intent.asks_no_changes(text))
        for text in ("", "Фон море", "нет, замените фон", "оставить лицо, фон заменить", "улучшить качество",
                     "на ваше усмотрение", "как есть, но поярче", "Оставьте все как есть, но фон замените",
                     "Хочу посмотреть макет, фон замените"):
            with self.subTest(text):
                self.assertFalse(wish_intent.asks_no_changes(text))

    def job(self, *, kind="portrait_background", size=(2000, 4000), count=1, exif=None, **fields):
        number = next(ORDERS)
        instance, item, chat, _ = self.setup_instance(kind, posting=f"ORDER-{number}", external_chat=f"CHAT-{number}")
        graph = {"nodes": [{"id": "make", "kind": "image_worker", "prompt": "Обработать", "next": "end", "error": "end"},
                           {"id": "end", "kind": "end"}]}
        with db.transaction() as con:
            photos = [media.store(con, picture(size[0] + index, size[1], kind="JPEG", exif=exif), {}, chat, item)
                      for index in range(count)]
            con.execute("UPDATE versions SET graph=? WHERE id=(SELECT version_id FROM instances WHERE id=?)",
                        (db.dump(graph), instance))
            con.execute("UPDATE instances SET node='make',status='collecting',prompted=0,fields=? WHERE id=?",
                        (db.dump({"photos": photos, **fields}), instance))
            con.execute("UPDATE outbox SET state='sent'")
            with patch("app.image_tasks.configured", return_value=True):
                scenarios.advance(con, instance)
            row = con.execute("SELECT * FROM image_jobs WHERE instance_id=?", (instance,)).fetchone()
            audit = [r[0] for r in con.execute("SELECT action FROM audit WHERE chat_id=?", (chat,))]
            return row, audit

    def test_large_photo_without_wishes_is_printed_as_is(self):
        row, audit = self.job()
        self.assertEqual(row["skip_generation"], 1)
        self.assertIn("image_job.generation_skipped", audit)
        with db.transaction() as con:
            pulled = image_tasks.claim_next(con)
        self.assertIs(pulled["skip_generation"], True)
        self.assertEqual(len(pulled["image_urls"]), 1)

    def test_job_carries_the_article_and_order_number_for_the_print_labels(self):
        row, _ = self.job()
        with db.transaction() as con:
            con.execute("UPDATE items SET offer_id='ТЕСТ-1',order_number='12345678-0001' WHERE id=?", (row["item_id"],))
            pulled = image_tasks.claim_next(con)
        self.assertEqual((pulled["offer_id"], pulled["order_number"]), ("ТЕСТ-1", "12345678-0001"))

    def test_print_label_falls_back_to_the_posting_without_an_order_number(self):
        row, _ = self.job()
        with db.transaction() as con:
            con.execute("UPDATE items SET order_number='' WHERE id=?", (row["item_id"],))
            posting = con.execute("SELECT posting FROM items WHERE id=?", (row["item_id"],)).fetchone()[0]
            pulled = image_tasks.claim_next(con)
        self.assertEqual(pulled["order_number"], posting)

    def test_buyer_refusing_changes_still_skips(self):
        self.assertEqual(self.job(background="Фон не менять", wishes="Без изменений")[0]["skip_generation"], 1)

    def test_generation_stays_when_it_is_needed(self):
        cases = {
            "small photo": {"size": (900, 1100)},
            "background wish": {"background": "Море"},
            "other wish": {"wishes": "Убрать прохожего"},
            "caption": {"caption": "С юбилеем"},
            "collage": {"kind": "collage"},
            "template": {"kind": "template_art"},
            "two photos": {"count": 2},
        }
        for name, options in cases.items():
            with self.subTest(name):
                row, audit = self.job(**options)
                self.assertEqual(row["skip_generation"], 0)
                self.assertNotIn("image_job.generation_skipped", audit)
                with db.transaction() as con:
                    self.assertIs(image_tasks.claim_next(con)["skip_generation"], False)
                    con.execute("UPDATE image_jobs SET state='failed' WHERE id=?", (row["id"],))

    def test_exif_rotation_is_counted_like_the_worker_does(self):
        exif = Image.Exif()
        exif[0x0112] = 6
        # Stored as landscape, shown as portrait: fits a portrait canvas.
        self.assertEqual(self.job(size=(4000, 2000), exif=exif)[0]["skip_generation"], 1)
        self.assertEqual(self.job(size=(4000, 2000))[0]["skip_generation"], 0)

    def test_unreadable_photo_keeps_generation(self):
        _, item, chat, _ = self.setup_instance()
        with db.transaction() as con:
            photo = media.store(con, picture(2000, 4000), {}, chat, item)
            (db.DATA / "media" / photo).write_bytes(b"broken")
            self.assertTrue(image_tasks.generation_needed(con, item, [photo], {}, 40, 60))

    def test_manager_rework_always_generates(self):
        row, _ = self.job()
        with db.transaction() as con:
            con.execute("UPDATE image_jobs SET state='completed' WHERE id=?", (row["id"],))
            with patch("app.image_tasks.configured", return_value=True):
                task = image_tasks.enqueue(con, row["item_id"], json.loads(row["media_ids"]), "Доработать", 40, 60, "2",
                                           node_id="rework:abc", manager_selected=True, chat_id=row["chat_id"]
                                           or con.execute("SELECT chat_id FROM media WHERE id=?", (row["media_id"],)).fetchone()[0])
            self.assertEqual(con.execute("SELECT skip_generation FROM image_jobs WHERE id=?", (task,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
