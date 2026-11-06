import unittest

from app import db, presentation, worker
from tests import test_folio


class ItemPriceTest(unittest.TestCase):
    setUp = test_folio.FolioTest.setUp
    tearDown = test_folio.FolioTest.tearDown
    setup_instance = test_folio.FolioTest.setup_instance

    def setup_chat(self):
        instance, item, chat, scenario = self.setup_instance()
        with db.transaction() as con:
            con.execute("INSERT INTO assignments VALUES(?,'2')", (chat,))
            row = con.execute("SELECT * FROM items WHERE id=?", (item,)).fetchone()
        return item, chat, row

    def sync(self, row, **fields):
        with db.transaction() as con:
            worker.import_order(con, 1, {
                "posting_number": row["posting"], "status": "awaiting_packaging",
                "products": [{"sku": row["sku"], "offer_id": row["offer_id"], "name": row["name"],
                              "quantity": fields.pop("quantity", 1), **fields}],
            })

    def price(self, item):
        with db.transaction() as con:
            stored = con.execute("SELECT price,currency FROM items WHERE id=?", (item,)).fetchone()
            return stored["price"], stored["currency"]

    def page(self, chat):
        return self.client.get(f"/chats/{chat}", headers=self.manager).text

    def test_price_from_ozon_is_stored_and_shown(self):
        item, chat, row = self.setup_chat()
        self.sync(row, price="1259.0000", currency_code="RUB")
        self.assertEqual(self.price(item), ("1259.0000", "RUB"))
        self.assertIn("цена 1 259 ₽", self.page(chat))

    def test_several_pieces_show_the_price_per_piece(self):
        item, chat, row = self.setup_chat()
        self.sync(row, price="990.50", currency_code="RUB", quantity=2)
        self.assertIn("2 шт. · цена 990,50 ₽ за шт.", self.page(chat))

    def test_missing_or_broken_price_does_not_stop_the_order(self):
        for value in (None, "", "бесплатно", "0", "-5", "NaN", "Infinity"):
            with self.subTest(value):
                item, chat, row = self.setup_chat()
                self.sync(row, price=value, currency_code="RUB")
                self.assertEqual(self.price(item), (None, None))
                self.assertIn("цена не указана", self.page(chat))
                self.tearDown()
                self.setUp()

    def test_later_sync_without_a_price_keeps_the_known_one(self):
        item, _, row = self.setup_chat()
        self.sync(row, price="1000", currency_code="RUB")
        self.sync(row)
        self.assertEqual(self.price(item), ("1000", "RUB"))
        self.sync(row, price="1200", currency_code="RUB")
        self.assertEqual(self.price(item), ("1200", "RUB"))

    def test_money_format(self):
        for price, currency, expected in (
            ("1259.0000", "RUB", "1 259 ₽"),
            ("1259.5", "RUB", "1 259,50 ₽"),
            ("990", "RUB", "990 ₽"),
            ("1000000", "RUB", "1 000 000 ₽"),
            ("15.5", "CNY", "15,50 CNY"),
            ("15", "", "15"),
            ("15", None, "15"),
        ):
            with self.subTest(price):
                self.assertEqual(presentation.format_money(price, currency), expected)

    def test_upgrade_keeps_existing_orders_without_a_price(self):
        item, chat, _ = self.setup_chat()
        db.initialize()
        self.assertEqual(self.price(item), (None, None))
        self.assertIn("цена не указана", self.page(chat))


if __name__ == "__main__":
    unittest.main()
