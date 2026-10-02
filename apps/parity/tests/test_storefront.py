import os
from pathlib import Path
from tempfile import TemporaryDirectory
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

os.environ.setdefault("PARITY_DATA_DIR", "/tmp/parity-tests-import")
from app.config import Settings
from app.db import migrate
from app.main import create_app
from app.models import Account, ActiveJob, CollectionAttempt, Comparison, Job, Mapping, Policy, Product, Snapshot
from app.queries import catalog, history
from app.service import automatic_mappings, claim, enqueue, run_job
from app.storefront import CollectionError, EXTRACT, Observation, StorefrontCollector, normalize_observation, product_url, rubles
from app.domain import NormalizedPrice, NormalizedProduct
from app.adapters import OzonAdapter, WildberriesAdapter


class ParserTests(unittest.TestCase):
    def test_localized_rubles_are_exact(self):
        self.assertEqual(rubles("3\u2009103\u2009₽"), Decimal("3103"))
        self.assertEqual(rubles("1\u00a0234,56 ₽"), Decimal("1234.56"))
        for value in ("от 3103 ₽", "3103 ₽ 3447 ₽", "0 ₽", "NaN ₽", "100 $", "-1 ₽"):
            with self.subTest(value=value), self.assertRaises(CollectionError):
                rubles(value)

    def test_ozon_uses_buyer_sku_never_seller_id(self):
        self.assertEqual(product_url("ozon", "3244447271", "3267204518"), "https://www.ozon.ru/product/3267204518/")
        for value in (None, "../123", "https://127.0.0.1", "0", "123?x=1"):
            with self.assertRaises(CollectionError):
                product_url("ozon", "3244447271", value)

    def test_identity_region_benefit_fail_closed(self):
        valid = dict(identity="123", city="Москва", hasBox=True, benefitConfirmed=True, benefit="100 ₽", regular="200 ₽", reference="500 ₽")
        result = normalize_observation(valid, "wb", "123", "Москва", "url")
        self.assertEqual(result.price.loyalty_price, Decimal("100"))
        for changes, expected in [({"blocked": True}, "blocked"), ({"city": "Казань"}, "region"), ({"identity": "124"}, "identity"), ({"benefitConfirmed": False}, "benefit_missing"), ({"hasBox": False}, "layout")]:
            with self.assertRaises(CollectionError) as raised:
                normalize_observation({**valid, **changes}, "wb", "123", "Москва", "url")
            self.assertEqual(raised.exception.code, expected)


class StorefrontJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        directory = Path(self.temp.name)
        self.settings = Settings(directory, f"sqlite:///{directory / 'test.db'}", "/parity", "")
        self.app = create_app(self.settings)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.factory = self.app.state.factory
        self.headers = {"X-Bellenne-User-Id": "1", "X-Bellenne-Csrf-Token": "csrf"}
        self.client.get("/", headers=self.headers)
        with self.factory() as session:
            for account in session.scalars(select(Account)):
                session.add(Product(owner_id=1, account_id=account.id, marketplace=account.marketplace, external_id="123" if account.marketplace == "wb" else "456", sku="123" if account.marketplace == "wb" else "789", seller_article="TEST", name="Тест", status="active"))
            session.flush()
            automatic_mappings(session, 1, None)
            session.commit()
        self.calls = []

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def run_collection(self, errors=None, target=None, kind="sync", marketplace=None, evidence=None):
        parent = self
        class Fake:
            def collect(self, product):
                parent.calls.append(product.id)
                if errors and product.marketplace in errors:
                    error = errors[product.marketplace]
                    raise error if isinstance(error, CollectionError) else CollectionError(error)
                return Observation(NormalizedPrice(Decimal("10000"), Decimal("4000"), Decimal("2917") if product.marketplace == "wb" else Decimal("3043"), "RUB", {}, {}), product_url(product.marketplace, product.external_id, product.sku), {"city": "Москва", "session": "guest", "payment": "wb_wallet" if product.marketplace == "wb" else "ozon_card"}, evidence)
            def close(self):
                pass
        with self.factory() as session:
            job = enqueue(session, 1, marketplace, kind, target_product_id=target)
            session.commit()
            job_id = job.id
        claim(self.factory, self.settings)
        run_job(self.factory, self.settings, job_id, collector_factory=lambda *args: Fake())
        return job_id

    def test_prices_compare_only_benefit_and_record_context(self):
        job_id = self.run_collection()
        with self.factory() as session:
            job = session.get(Job, job_id)
            self.assertEqual((job.status, job.products_received, job.prices_updated), ("success", 2, 2))
            self.assertEqual(session.scalar(select(Comparison).where(Comparison.price_type == "loyalty_price")).signed_difference, Decimal("126"))
            self.assertEqual(session.scalar(select(Comparison).where(Comparison.price_type == "base_price")).status, "not_compared")
            self.assertEqual(session.scalar(select(Snapshot)).context["city"], "Москва")
            self.assertEqual(session.scalar(select(func.count()).select_from(CollectionAttempt)), 2)
            self.assertEqual(catalog(session, 1, status="mismatch")["total"], 1)

    def test_failed_page_keeps_history_but_disables_comparison(self):
        self.run_collection()
        self.run_collection(errors={"wb": "region"})
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 3)
            self.assertEqual(session.scalar(select(Mapping)).status, "stale")
            self.assertEqual(catalog(session, 1, status="mismatch")["total"], 0)
            self.assertEqual(session.scalar(select(Product).where(Product.marketplace == "wb")).collection_error, "region")

    def test_block_stops_marketplace_but_other_market_runs(self):
        with self.factory() as session:
            account = session.scalar(select(Account).where(Account.marketplace == "wb"))
            session.add(Product(owner_id=1, account_id=account.id, marketplace="wb", external_id="124", sku="124", name="Test 2", seller_article="OTHER", status="active"))
            session.commit()
        job_id = self.run_collection(errors={"wb": "blocked"})
        self.assertEqual(len(self.calls), 2)
        with self.factory() as session:
            job = session.get(Job, job_id)
            self.assertEqual(job.status, "partial")
            self.assertEqual(job.results["wb"]["skipped"], 1)
            self.assertEqual(job.errors_count, 2)
            self.assertIsNone(session.get(ActiveJob, 1))

    def test_target_and_probe_are_bounded(self):
        with self.factory() as session:
            product = session.scalar(select(Product).where(Product.marketplace == "ozon"))
            target = product.id
        self.run_collection(target=target, marketplace="ozon")
        self.assertEqual(self.calls, [target])
        self.calls.clear()
        self.run_collection(kind="probe", marketplace="wb")
        self.assertEqual(len(self.calls), 1)

    def test_shared_errors_display_once_in_new_and_historical_jobs(self):
        from app.storefront import ERRORS
        job_id = self.run_collection(errors={"wb": "blocked", "ozon": "blocked"})
        message = ERRORS["blocked"]
        with self.factory() as session:
            job = session.get(Job, job_id)
            self.assertEqual(job.error_summary.count(message), 1)
            self.assertIn("Ozon", job.error_summary)
            self.assertIn("Wildberries", job.error_summary)
            # Old persisted jobs must render correctly without rewriting history.
            job.error_summary = message + " " + message
            session.commit()
        api = self.client.get(f"/api/jobs/{job_id}", headers=self.headers).json()
        self.assertEqual(api["error_summary"].count(message), 1)
        html = self.client.get(f"/jobs/{job_id}", headers=self.headers).text
        self.assertEqual(html.count(message), 1)
        with self.factory() as session:
            self.assertEqual(session.get(Job, job_id).error_summary.count(message), 2)

    def test_network_failure_stops_remaining_pages_and_records_safe_code(self):
        with self.factory() as session:
            account = session.scalar(select(Account).where(Account.marketplace == "wb"))
            session.add(Product(owner_id=1, account_id=account.id, marketplace="wb", external_id="124", sku="124", name="Test 2", seller_article="OTHER", status="active"))
            session.commit()
        job_id = self.run_collection(errors={"wb": CollectionError("navigation", {"browser_error": "ERR_CONNECTION_RESET"})})
        self.assertEqual(len(self.calls), 2)
        with self.factory() as session:
            job = session.get(Job, job_id)
            self.assertEqual(job.results["wb"]["skipped"], 1)
            attempt = session.scalar(select(CollectionAttempt).where(CollectionAttempt.error_code == "navigation").order_by(CollectionAttempt.id))
            self.assertEqual(attempt.details["browser_error"], "ERR_CONNECTION_RESET")

    def test_ui_target_owner_csrf_and_evidence(self):
        job_id = self.run_collection(evidence=b"fixture-jpeg")
        with self.factory() as session:
            snapshot_id = session.scalar(select(Snapshot.id))
            mapping_id = session.scalar(select(Mapping.id))
        self.assertEqual(self.client.get(f"/observations/{snapshot_id}", headers=self.headers).content, b"fixture-jpeg")
        self.assertEqual(self.client.get(f"/observations/{snapshot_id}", headers={**self.headers, "X-Bellenne-User-Id": "2"}).status_code, 404)
        self.assertEqual(self.client.post("/jobs", headers=self.headers, data={"target_product_id": 9999, "csrf_token": "csrf"}).status_code, 404)
        self.assertEqual(self.client.post("/storefront", headers=self.headers, data={"page_interval_seconds": 5}).status_code, 422)
        self.assertEqual(self.client.post("/storefront", headers=self.headers, data={"page_interval_seconds": 1, "csrf_token": "csrf"}).status_code, 422)
        for path in ("/integrations", "/products", "/mappings", "/discrepancies", f"/products/{mapping_id}", f"/jobs/{job_id}"):
            self.assertEqual(self.client.get(path, headers=self.headers).status_code, 200, path)

    def test_legacy_prices_hidden_and_history_not_joined(self):
        self.run_collection()
        with self.factory() as session:
            product = session.scalar(select(Product).where(Product.marketplace == "wb"))
            snapshot = Snapshot(product_id=product.id, job_id=enqueue(session, 1).id, source_kind="seller_api", context_version=0, currency="RUB", base_price=5421, discount_price=5421, sources={}, reasons={})
            session.add(snapshot)
            session.flush()
            product.current_snapshot_id = snapshot.id
            product.price_source = "seller_api"
            policy = session.get(Policy, 1)
            policy.context_version = 2
            session.commit()
            mapping = session.scalar(select(Mapping))
            data = catalog(session, 1)
            self.assertIsNone(data["rows"][0]["wb_price"])
            self.assertIsNone(data["rows"][0]["ozon_price"])
            timeline = history(session, mapping, 0)
            self.assertTrue(all(change["initial"] for change in timeline["changes"].values()))
            self.assertEqual(history(session, mapping, 0, source_kind="seller_api")["total"], 1)
            self.assertEqual(history(session, mapping, 0, source_kind="storefront", context_version=2)["total"], 0)

    def test_catalog_import_preserves_prices_and_ignores_api_money(self):
        self.run_collection()
        class Catalog:
            def products(self, include_prices=True):
                assert include_prices is False
                yield NormalizedProduct("123", "TEST", "Новое имя", "123", "active", NormalizedPrice(base_price=Decimal("9999")))
            def close(self):
                pass
        with self.factory() as session:
            account = session.scalar(select(Account).where(Account.marketplace == "wb"))
            from app.security import cipher
            account.secret = cipher(self.settings).encrypt(b"key").decode()
            job = enqueue(session, 1, "wb", "catalog")
            session.commit()
            job_id = job.id
        claim(self.factory, self.settings)
        run_job(self.factory, self.settings, job_id, adapter_factory=lambda a: Catalog())
        with self.factory() as session:
            product = session.scalar(select(Product).where(Product.marketplace == "wb"))
            self.assertEqual(product.name, "Новое имя")
            self.assertEqual(session.get(Snapshot, product.current_snapshot_id).loyalty_price, Decimal("2917"))
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 2)

    def test_migration_from_v1_is_additive_and_pauses_schedule_once(self):
        self.run_collection()
        with self.factory() as session:
            session.execute(text("DROP TABLE collection_attempts"))
            session.execute(text("DROP INDEX ix_price_snapshots_source_kind"))
            columns = {"sync_policies": ["price_source", "city", "price_basis", "context_version", "page_interval_seconds"], "marketplace_products": ["price_source", "price_context_version", "collection_status", "collection_error"], "price_snapshots": ["source_kind", "context_version", "context", "page_url", "evidence_key"]}
            for table, names in columns.items():
                for name in names:
                    session.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))
            session.execute(text("DELETE FROM schema_migrations WHERE version=2"))
            session.execute(text("UPDATE sync_policies SET enabled=1"))
            session.commit()
        migrate(self.app.state.factory.kw["bind"])
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 2)
            self.assertEqual(session.scalar(select(Snapshot)).source_kind, "seller_api")
            self.assertFalse(session.get(Policy, 1).enabled)
            session.get(Policy, 1).enabled = True
            session.commit()
        migrate(self.app.state.factory.kw["bind"])
        with self.factory() as session:
            self.assertTrue(session.get(Policy, 1).enabled)


class BrowserContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright, Error
        cls.runtime = sync_playwright().start()
        try:
            cls.browser = cls.runtime.chromium.launch()
        except Error:
            cls.runtime.stop()
            raise unittest.SkipTest("Chromium required: run browser-test Docker target")
        cls.page = cls.browser.new_page()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def test_wb_dynamic_buy_box_excludes_recommendations(self):
        self.page.set_content('''<header><a>Москва</a></header><table><tr><td>Артикул</td><td>701955470</td></tr></table><div id="root"></div><p>Рекомендуем 1 ₽</p><script>setTimeout(()=>document.querySelector('#root').innerHTML='<div class="priceBlockPriceWrap--new"><button class="priceBlockWalletPrice--new"><h2>2&nbsp;958&nbsp;₽</h2></button><ins class="priceBlockFinalPrice--new">3&nbsp;019&nbsp;₽</ins><span class="priceBlockOldPrice--new">15&nbsp;060&nbsp;₽</span></div>',150)</script>''')
        self.page.locator('[class*="priceBlockWalletPrice--"]').wait_for()
        data = self.page.evaluate(EXTRACT, {"marketplace": "wb"})
        result = normalize_observation(data, "wb", "701955470", "Москва", "url")
        self.assertEqual(result.price.loyalty_price, Decimal("2958"))
        self.assertEqual(result.price.discount_price, Decimal("3019"))

    def test_ozon_banks_requires_explicit_ozon_card(self):
        html = '''<header><button>Москва</button></header><h1>Товар</h1><button>Артикул: 3267204518</button><div data-widget="webPrice"><button><span class="tsHeadline600Large">3 103 ₽</span><span>С банками</span><img src="https://ir.ozone.ru/s3/payments-cdn/wc75/ozon-price-compact-new-ozon-light.png"></button><span class="tsHeadline500Medium">3 447 ₽</span><span style="text-decoration:line-through">15 060 ₽</span><p>В рассрочку 500 ₽</p></div><p>Рекомендуем 1 ₽</p>'''
        self.page.set_content(html)
        data = self.page.evaluate(EXTRACT, {"marketplace": "ozon"})
        result = normalize_observation(data, "ozon", "3267204518", "Москва", "url")
        self.assertEqual(result.price.loyalty_price, Decimal("3103"))
        self.assertEqual(result.price.base_price, Decimal("15060"))
        self.page.set_content(html.replace("ozon-price-compact-new-ozon-light.png", "another-bank.png"))
        with self.assertRaises(CollectionError) as raised:
            normalize_observation(self.page.evaluate(EXTRACT, {"marketplace": "ozon"}), "ozon", "3267204518", "Москва", "url")
        self.assertEqual(raised.exception.code, "benefit_missing")

    def test_duplicate_price_or_hidden_city_not_accepted(self):
        self.page.set_content('''<header><a style="display:none">Москва</a></header><p>Москва</p><table><tr><td>Артикул</td><td>123</td></tr></table><div class="priceBlockPriceWrap--a"><button class="priceBlockWalletPrice--a">100 ₽</button><button class="priceBlockWalletPrice--b">90 ₽</button></div>''')
        data = self.page.evaluate(EXTRACT, {"marketplace": "wb"})
        self.assertIsNone(data["city"])
        self.assertIsNone(data["benefit"])

    def test_sold_out_main_card_is_unavailable_without_using_recommendations(self):
        self.page.set_content('<main><h1>Товар</h1><div>Нет в наличии</div></main>')
        data = self.page.evaluate(EXTRACT, {'marketplace': 'wb'})
        self.assertTrue(data['unavailable'])
        with self.assertRaises(CollectionError) as raised:
            normalize_observation(data, 'wb', '123', 'Москва', 'url', check_city=False)
        self.assertEqual(raised.exception.code, 'unavailable')
        self.page.set_content('<main><h1>Товар</h1><aside><p>Нет в наличии</p></aside></main>')
        self.assertFalse(self.page.evaluate(EXTRACT, {'marketplace': 'wb'})['unavailable'])
        self.page.set_content('<main><p>Нет в наличии</p><div class="priceBlockPriceWrap--test"><button class="priceBlockWalletPrice--test">100 ₽</button></div></main>')
        self.assertFalse(self.page.evaluate(EXTRACT, {'marketplace': 'wb'})['unavailable'])

    def test_real_collector_waits_for_dynamic_fixture_and_captures_evidence(self):
        html = b'<header><a>PLACE</a></header><table><tr><td>ARTICLE</td><td>123</td></tr></table><div id="root"></div>'
        html = html.decode().replace("PLACE", "Москва").replace("ARTICLE", "Артикул") + '''<script>setTimeout(()=>document.querySelector('#root').innerHTML='<div class="priceBlockPriceWrap--a"><button class="priceBlockWalletPrice--a">100 ₽</button></div>',200)</script>'''
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with TemporaryDirectory() as directory:
            settings = Settings(Path(directory), "sqlite://", "", "", http_timeout=5)
            collector = StorefrontCollector(settings, SimpleNamespace(city="Москва", page_interval_seconds=2))
            try:
                with patch("app.storefront.product_url", return_value=f"http://127.0.0.1:{server.server_port}/"):
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        result = executor.submit(collector.collect, SimpleNamespace(marketplace="wb", external_id="123", sku="123")).result()
                        executor.submit(collector.close).result()
                        collector = None
                self.assertEqual(result.price.loyalty_price, Decimal("100"))
                self.assertTrue(result.evidence.startswith(b"\xff\xd8"))
            finally:
                if collector:
                    collector.close()
                server.shutdown()
                server.server_close()
