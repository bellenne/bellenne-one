import json
import os
from html import unescape
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

os.environ.setdefault("PARITY_DATA_DIR", str(Path(os.getenv("TEMP", "/tmp")) / "parity-test-import"))
from app.adapters import ApiError, OzonAdapter, WildberriesAdapter
from app.config import Settings
from app.domain import NormalizedPrice, NormalizedProduct, now
from app.main import create_app
from app.models import Account, ActiveJob, Comparison, Event, Job, Mapping, Policy, Product, Snapshot
from app.queries import catalog, history
from app.security import account_view, cipher
from app.service import automatic_mappings, claim, enqueue, ensure_owner, manual_link, recalculate, run_job, schedule_due, unlink, write_lock


def product(external, article="ABC-123", value="1000", loyalty=None, currency="RUB"):
    return NormalizedProduct(str(external), article, "Картина " + article, str(external) + "sku", "active", NormalizedPrice(
        Decimal(value) if value is not None else None, Decimal(value) if value is not None else None,
        Decimal(loyalty) if loyalty is not None else None, currency, {"base_price": "documented", "discount_price": "documented"},
        {"loyalty_price": "not_supported"} if loyalty is None else {}))


class FakeAdapter:
    def __init__(self, products=(), error=None, capabilities=None):
        self.values, self.error = products, error
        self.capabilities = capabilities or {"products": {"status": "available"}, "prices": {"status": "available"}}

    def products(self):
        yield from self.values
        if self.error:
            raise ApiError(self.error)

    def probe(self):
        return self.capabilities

    def close(self):
        pass


class ParityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        directory = Path(self.temp.name)
        self.settings = Settings(directory, f"sqlite:///{directory / 'test.db'}", "/parity", "")
        self.app = create_app(self.settings)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.factory = self.app.state.factory
        self.headers = {"X-Bellenne-User-Id": "1", "X-Bellenne-Username": "tester", "X-Bellenne-Csrf-Token": "csrf"}
        self.client.get("/", headers=self.headers)
        with self.factory() as session:
            session.get(Policy, 1).price_source = "seller_api"
            for a in session.scalars(select(Account)):
                a.secret = cipher(self.settings).encrypt(b"TOP-SECRET-CREDENTIAL").decode()
                a.client_id = "123"
            session.commit()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def sync(self, wb=(), ozon=(), wb_error=None, ozon_error=None, marketplace=None):
        with self.factory() as session:
            write_lock(session)
            job = enqueue(session, 1, marketplace)
            session.commit()
            job_id = job.id
        self.assertEqual(claim(self.factory, self.settings), job_id)
        run_job(self.factory, self.settings, job_id, lambda a: FakeAdapter(wb if a.marketplace == "wb" else ozon, wb_error if a.marketplace == "wb" else ozon_error))
        return job_id

    def mapping(self):
        with self.factory() as session:
            return session.scalar(select(Mapping).where(Mapping.wb_product_id.is_not(None), Mapping.ozon_product_id.is_not(None)))

    def test_exact_article_automatic_and_equal_prices(self):
        self.sync([product(1)], [product(2)])
        mapping = self.mapping()
        self.assertEqual(mapping.mapping_type, "automatic")
        self.assertEqual(mapping.status, "matched")

    def test_different_articles_never_auto_link(self):
        self.sync([product(1, "A")], [product(2, "B")])
        self.assertIsNone(self.mapping())

    def test_ambiguous_articles_require_mapping(self):
        self.sync([product(1), product(3)], [product(2)])
        self.assertIsNone(self.mapping())
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Mapping).where(Mapping.status == "mapping_required")), 3)

    def test_new_duplicate_splits_previous_auto_mapping(self):
        self.sync([product(1)], [product(2)])
        self.sync([product(1), product(3)], [product(2)])
        self.assertIsNone(self.mapping())

    def test_manual_mapping_preserved(self):
        self.sync([product(1, "A")], [product(2, "B")])
        with self.factory() as session:
            wb, ozon = list(session.scalars(select(Product).order_by(Product.id)))
            linked = manual_link(session, 1, wb.id, ozon.id)
            session.commit()
            identity = linked.id
        self.sync([product(1, "A"), product(3, "B")], [product(2, "B")])
        mapping = self.mapping()
        self.assertEqual((mapping.id, mapping.mapping_type), (identity, "manual"))

    def test_changed_article_invalidates_auto_mapping(self):
        self.sync([product(1)], [product(2)])
        self.sync([product(1, "CHANGED")], [product(2)])
        self.assertIsNone(self.mapping())

    def test_unlink_not_recreated_by_sync(self):
        self.sync([product(1)], [product(2)])
        mapping = self.mapping()
        with self.factory() as session:
            unlink(session, session.get(Mapping, mapping.id))
            recalculate(session, 1)
            session.commit()
        self.sync([product(1)], [product(2)])
        self.assertIsNone(self.mapping())

    def test_manual_replacement_keeps_displaced_products(self):
        self.sync([product(1, "A"), product(3, "B")], [product(2, "A"), product(4, "B")])
        with self.factory() as session:
            wb = session.scalar(select(Product).where(Product.external_id == "1"))
            ozon = session.scalar(select(Product).where(Product.external_id == "4"))
            manual_link(session, 1, wb.id, ozon.id)
            session.commit()
            self.assertEqual(session.scalar(select(func.count()).select_from(Mapping)), 3)

    def test_difference_100_rubles_10_percent(self):
        self.sync([product(1)], [product(2, value="1100")])
        self.assertEqual(self.mapping().status, "mismatch")
        with self.factory() as session:
            c = session.scalar(select(Comparison).where(Comparison.price_type == "discount_price"))
            self.assertEqual((c.difference_absolute, c.difference_percent, c.direction), (Decimal("100"), Decimal("10"), "wb_cheaper"))

    def test_zero_base_has_no_infinite_percent(self):
        self.sync([product(1, value="0")], [product(2, value="10")])
        with self.factory() as session:
            c = session.scalar(select(Comparison).where(Comparison.price_type == "base_price"))
            self.assertIsNone(c.difference_percent)

    def test_missing_loyalty_is_not_zero(self):
        self.sync([product(1)], [product(2, loyalty="900")])
        with self.factory() as session:
            c = session.scalar(select(Comparison).where(Comparison.price_type == "loyalty_price"))
            self.assertEqual(c.status, "unavailable")
            self.assertIsNone(c.difference_absolute)
            self.assertIsNone(session.scalar(select(Snapshot).join(Product).where(Product.marketplace == "wb")).loyalty_price)

    def test_all_missing_prices_unavailable(self):
        self.sync([product(1, value=None)], [product(2, value=None)])
        self.assertEqual(self.mapping().status, "price_unavailable")

    def test_currencies_not_compared(self):
        self.sync([product(1)], [product(2, currency="USD")])
        self.assertEqual(self.mapping().status, "price_unavailable")

    def test_partial_keeps_prices_and_marks_stale(self):
        self.sync([product(1)], [product(2)])
        job_id = self.sync([], [product(2, value="1200")], wb_error="auth")
        with self.factory() as session:
            self.assertEqual(session.get(Job, job_id).status, "partial")
            wb = session.scalar(select(Product).where(Product.marketplace == "wb"))
            self.assertEqual(session.get(Snapshot, wb.current_snapshot_id).base_price, Decimal("1000"))
            self.assertTrue(wb.present)
            self.assertEqual(catalog(session, 1)["rows"][0]["status"], "stale")

    def test_mid_import_failure_keeps_whole_marketplace_catalog(self):
        self.sync([product(1)], [product(2)])
        self.sync([product(1, value="900"), product(3)], [product(2)], wb_error="temporary")
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Product).where(Product.marketplace == "wb")), 1)
            wb = session.scalar(select(Product).where(Product.marketplace == "wb"))
            self.assertEqual(session.get(Snapshot, wb.current_snapshot_id).base_price, Decimal("1000"))

    def test_identical_sync_no_duplicates_or_history_spam(self):
        self.sync([product(1)], [product(2)])
        self.sync([product(1)], [product(2)])
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Product)), 2)
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 2)
            self.assertEqual(session.scalar(select(func.count()).select_from(Job)), 2)

    def test_daily_snapshot_even_when_unchanged(self):
        self.sync([product(1)], [product(2)])
        with self.factory() as session:
            for snapshot in session.scalars(select(Snapshot)):
                snapshot.captured_at = now() - timedelta(days=1)
            session.commit()
        self.sync([product(1)], [product(2)])
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 4)

    def test_intraday_changes_and_history_sorted_with_gaps(self):
        self.sync([product(1)], [product(2)])
        self.sync([product(1, value=None)], [product(2, value="1200")])
        with self.factory() as session:
            data = history(session, session.get(Mapping, self.mapping().id), 7)
            dates = [d["captured_at"] for d in data["chart"]]
            self.assertEqual(dates, sorted(dates))
            self.assertTrue(any(d["base_price"] is None for d in data["chart"]))

    def test_catalog_server_search_and_pagination(self):
        self.sync([product(i, f"ART-{i}") for i in range(1, 65)], [product(1000+i, f"ART-{i}") for i in range(1, 65)])
        with self.factory() as session:
            for q in ("ART-64", "64sku", "1064sku", "1064"):
                data = catalog(session, 1, q=q, exact=True)
                self.assertEqual(data["total"], 1)
            self.assertEqual(len(catalog(session, 1)["rows"]), 50)
            self.assertEqual(len(catalog(session, 1, page=2)["rows"]), 14)

    def test_money_exact_storage(self):
        self.sync([product(1, value="999999999.99")], [product(2, value="999999999.98")])
        with self.factory() as session:
            s = session.scalar(select(Snapshot).order_by(Snapshot.id))
            self.assertIsInstance(s.base_price, Decimal)
            self.assertEqual(s.base_price, Decimal("999999999.99"))
            self.assertEqual(session.scalar(text("SELECT typeof(base_price) FROM price_snapshots LIMIT 1")), "integer")

    def test_catalog_refreshes_stale_with_time(self):
        self.sync([product(1)], [product(2)])
        with self.factory() as session:
            for p in session.scalars(select(Product)):
                p.updated_at = now() - timedelta(days=2)
            session.commit()
            self.assertEqual(catalog(session, 1, status="stale")["total"], 1)
            self.assertEqual(catalog(session, 1, status="matched")["total"], 0)

    def test_detail_page_hides_current_comparison_when_stale(self):
        self.sync([product(1)], [product(2, value="1100")])
        mapping = self.mapping()
        with self.factory() as session:
            for p in session.scalars(select(Product)):
                p.updated_at = now() - timedelta(days=2)
            session.commit()
        html = self.client.get(f"/products/{mapping.id}", headers=self.headers).text
        self.assertIn("Данные устарели", html)
        self.assertNotIn("WB дешевле", html)

    def test_new_credentials_mark_old_comparison_stale_until_sync(self):
        self.sync([product(1)], [product(2)])
        self.client.post("/integrations/wb",headers=self.headers,data={"csrf_token":"csrf","api_key":"CHANGED-KEY"})
        with self.factory() as session:
            self.assertEqual(catalog(session,1,status="stale")["total"],1)
        self.sync([product(1)], [product(2)])
        self.assertEqual(self.mapping().status,"matched")

    def test_price_history_shows_exact_previous_values(self):
        self.sync([product(1)], [product(2)])
        self.sync([product(1,value="900")], [product(2)])
        with self.factory() as session:
            data=history(session,session.get(Mapping,self.mapping().id))
            changed=[d for d in data['changes'].values() if d['values']]
            change=changed[0]['values'][0]
            self.assertEqual((change['old'],change['new'],change['difference'],change['percent']), (Decimal('1000'),Decimal('900'),Decimal('-100'),Decimal('-10')))

    def test_blank_and_invalid_percent_filter(self):
        self.assertEqual(self.client.get('/discrepancies?min_percent=',headers=self.headers).status_code,200)
        self.assertEqual(self.client.get('/discrepancies?min_percent=NaN',headers=self.headers).status_code,422)

    def test_missing_product_after_complete_catalog(self):
        self.sync([product(1)], [product(2)])
        self.sync([], [product(2)])
        self.assertEqual(self.mapping().status, "missing_wb")

    def test_api_credentials_are_never_serialized(self):
        response = self.client.get("/api/integrations", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("TOP-SECRET", response.text)
        self.assertNotIn('"secret":', response.text)
        self.assertEqual(response.json()[0]["secret_masked"], "••••••••")

    def test_ui_save_encrypts_preserves_and_masks_secret(self):
        result = self.client.post("/integrations/wb", headers=self.headers, data={"csrf_token": "csrf", "api_key": "NEW-PRIVATE-TOKEN"}, follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        with self.factory() as session:
            a = session.scalar(select(Account).where(Account.marketplace == "wb"))
            self.assertNotIn("NEW-PRIVATE", a.secret)
            self.assertEqual(cipher(self.settings).decrypt(a.secret.encode()), b"NEW-PRIVATE-TOKEN")
        html = self.client.get("/integrations", headers=self.headers).text
        self.assertNotIn("NEW-PRIVATE", html)
        self.assertIn('type="password"', html)
        self.assertEqual(self.client.post("/integrations/wb", headers=self.headers, data={"csrf_token": "csrf", "api_key": ""}, follow_redirects=False).status_code, 303)

    def test_separate_capability_probe(self):
        response = self.client.post("/jobs", headers={**self.headers, "Accept": "application/json"}, data={"csrf_token": "csrf", "marketplace": "ozon", "kind": "probe"})
        self.assertEqual(response.status_code, 202)
        job_id = claim(self.factory, self.settings)
        run_job(self.factory, self.settings, job_id, lambda a: FakeAdapter(capabilities={"products": {"status": "available"}, "prices": {"status": "unavailable", "error": "auth"}}))
        with self.factory() as session:
            a = session.scalar(select(Account).where(Account.marketplace == "ozon"))
            self.assertEqual(a.capabilities["products"]["status"], "available")
            self.assertEqual(a.capabilities["prices"]["status"], "unavailable")
            self.assertIsNone(a.last_successful_sync_at)

    def test_csrf_auth_and_owner_isolation(self):
        self.assertEqual(self.client.get("/products").status_code, 401)
        self.assertEqual(self.client.post("/jobs", headers=self.headers, data={"csrf_token": "bad"}).status_code, 403)
        job_id = self.sync([product(1)], [product(2)])
        other = {**self.headers, "X-Bellenne-User-Id": "2"}
        self.client.get("/", headers=other)
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}", headers=other).status_code, 404)
        self.assertEqual(self.client.get(f"/products/{self.mapping().id}", headers=other).status_code, 404)
        self.assertEqual(self.client.get("/api/products?marketplace=wb", headers=other).json()["total"], 0)
        self.assertEqual(self.client.post(f"/mappings/{self.mapping().id}/unlink", headers=other, data={"csrf_token":"csrf"}).status_code, 404)

    def test_validation_never_echoes_submitted_key(self):
        response = self.client.post("/integrations/wb", headers=self.headers, data={"api_key":"PRIVATE-VALIDATION-DATA"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("PRIVATE-VALIDATION", response.text)

    def test_concurrent_enqueue_and_claim_once(self):
        def add():
            with self.factory() as session:
                write_lock(session)
                job = enqueue(session, 1)
                session.commit()
                return job.id
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(lambda _: add(), range(2)))
        self.assertEqual(jobs[0], jobs[1])
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: claim(self.factory, self.settings), range(2)))
        self.assertEqual(sum(j is not None for j in claims), 1)

    def test_settings_and_links_cannot_change_during_sync(self):
        self.sync([product(1)], [product(2)])
        mapping = self.mapping()
        with self.factory() as session:
            enqueue(session, 1)
            session.commit()
        for path, data in (
            ("/integrations/wb", {"api_key": "replacement"}),
            ("/schedule", {"daily_time": "02:00", "timezone_name": "Europe/Moscow"}),
            ("/mappings/link", {"wb_product_id": mapping.wb_product_id, "ozon_product_id": mapping.ozon_product_id}),
            (f"/mappings/{mapping.id}/unlink", {}),
        ):
            response = self.client.post(path, headers=self.headers, data={"csrf_token": "csrf", **data})
            self.assertEqual(response.status_code, 409, path)
        self.assertEqual(self.mapping().id, mapping.id)
        with self.factory() as session:
            current = session.scalar(select(Account).where(Account.marketplace == "wb"))
            self.assertEqual(cipher(self.settings).decrypt(current.secret.encode()), b"TOP-SECRET-CREDENTIAL")

    def test_schedule_normalizes_time_and_rejects_invalid_timezone(self):
        data = {"csrf_token": "csrf", "daily_time": "2:00", "timezone_name": "Europe/Moscow", "enabled": "true"}
        self.assertEqual(self.client.post("/schedule", headers=self.headers, data=data, follow_redirects=False).status_code, 303)
        with self.factory() as session:
            self.assertEqual(session.get(Policy, 1).daily_time, "02:00")
        data["timezone_name"] = "Unknown/Zone"
        self.assertEqual(self.client.post("/schedule", headers=self.headers, data=data).status_code, 422)

    def test_schedule_configurable_once_per_day(self):
        with self.factory() as session:
            p = session.get(Policy, 1)
            p.enabled, p.daily_time = True, "00:00"
            session.commit()
        schedule_due(self.factory)
        schedule_due(self.factory)
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Job)), 1)

    def test_interrupted_job_recovered_without_overwriting_data(self):
        with self.factory() as session:
            job = enqueue(session, 1)
            job.started_at = job.heartbeat_at = now() - timedelta(hours=1)
            session.commit()
            job_id = job.id
        self.assertIsNone(claim(self.factory, self.settings))
        with self.factory() as session:
            self.assertEqual(session.get(Job, job_id).status, "failed")
            self.assertIsNone(session.get(ActiveJob, 1))

    def test_render_all_pages_after_sync(self):
        job_id = self.sync([product(1)], [product(2, value="1100")])
        for path in ("/", "/integrations", "/products", "/discrepancies", "/mappings", f"/products/{self.mapping().id}", "/journal", f"/jobs/{job_id}"):
            response = self.client.get(path, headers=self.headers)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn("TOP-SECRET", response.text)

    def test_wb_api_price_fields_reach_database_catalog_and_chart(self):
        def handler(request):
            if 'content-api' in request.url.host:
                return httpx.Response(200, json={"cards": [{"nmID": 1, "vendorCode": "A", "title": "WB field contract"}], "cursor": {"total": 1}})
            return httpx.Response(200, json={"data": {"listGoods": [{"nmID": 1, "currencyIsoCode4217": "RUB", "sizes": [
                {"price": "2000", "discountedPrice": "1234.56", "clubDiscountedPrice": "1172.83"},
                {"price": "2000", "discountedPrice": "1234.56", "clubDiscountedPrice": "1172.83"},
            ]}]}})
        with self.factory() as session:
            job = enqueue(session, 1)
            session.commit()
            job_id = job.id
        self.assertEqual(claim(self.factory, self.settings), job_id)
        run_job(self.factory, self.settings, job_id, lambda account:
                WildberriesAdapter(self.settings, "test-only", transport=httpx.MockTransport(handler), sleep=lambda _: None)
                if account.marketplace == "wb" else FakeAdapter([product(2, "A", value="1100")]))
        with self.factory() as session:
            snapshot = session.scalar(select(Snapshot).join(Product, Snapshot.product_id == Product.id).where(Product.marketplace == "wb"))
            self.assertEqual(snapshot.discount_price, Decimal("1234.56"))
            self.assertEqual(snapshot.loyalty_price, Decimal("1172.83"))
            self.assertEqual(snapshot.sources["discount_price"], "sizes.discountedPrice")
            self.assertEqual(snapshot.sources["loyalty_price"], "sizes.clubDiscountedPrice")
        for path in ("/products", "/discrepancies", "/mappings"):
            html = self.client.get(path, headers=self.headers).text
            row = html.split('<tbody>')[1].split('</tbody>')[0]
            text_content = unescape(re.sub(r'<[^>]+>', ' ', row))
            self.assertRegex(text_content, r'Со скидкой\s+1 234\.56 ₽')
            self.assertRegex(text_content, r'Лояльность\s+1 172\.83 ₽')
        html = self.client.get(f"/products/{self.mapping().id}", headers=self.headers).text
        chart = json.loads(re.search(r'id="history-data">(.*?)</script>', html, re.S)[1])
        wb = next(point for point in chart if point['marketplace'] == 'wb')
        self.assertEqual(wb['discount_price'], '1234.56')
        self.assertEqual(wb['loyalty_price'], '1172.83')

    def test_catalog_html_pagination_preserves_filters(self):
        self.sync([product(i, f"ART & {i:03}") for i in range(121)],
                  [product(i+1000, f"ART & {i:03}", value="1100") for i in range(121)])
        for path in ("/products", "/discrepancies", "/mappings"):
            for page in (1, 2, 3):
                with self.subTest(path=path, page=page):
                    response = self.client.get(path, headers=self.headers, params={
                        "q": "ART &", "sort": "article", "page_size": 50, "page": page,
                        "price_type": "base_price", "direction": "wb_cheaper", "min_percent": "5",
                    })
                    self.assertEqual(response.status_code, 200)
                    navigation = response.text.split('aria-label="Страницы каталога"')[1].split('</nav>')[0]
                    links = [unescape(href) for href in re.findall(r'href="([^"]+)"', navigation)]
                    self.assertEqual(len(links), 2 if page == 2 else 1)
                    self.assertEqual({int(parse_qs(urlsplit(link).query)["page"][0]) for link in links},
                                     {2} if page != 2 else {1, 3})
                    for link in links:
                        self.assertEqual(urlsplit(link).path, "/parity" + path)
                        values = parse_qs(urlsplit(link).query)
                        self.assertEqual(values["q"], ["ART &"])
                        self.assertEqual(values["sort"], ["article"])
                        self.assertEqual(values["price_type"], ["base_price"])
                        self.assertEqual(values["direction"], ["wb_cheaper"])
                        self.assertEqual(values["min_percent"], ["5"])
                        self.assertEqual(values["page_size"], ["50"])

    def test_journal_html_pagination_preserves_job_filter(self):
        job_id = self.sync([product(i, f"A-{i}") for i in range(60)],
                           [product(i+1000, f"A-{i}") for i in range(60)])
        for page in (1, 2):
            response = self.client.get("/journal", headers=self.headers, params={"job_id": job_id, "page": page})
            self.assertEqual(response.status_code, 200)
            navigation = response.text.split('<nav class="pagination">')[1].split('</nav>')[0]
            links = [unescape(href) for href in re.findall(r'href="([^"]+)"', navigation)]
            self.assertTrue(links)
            for link in links:
                values = parse_qs(urlsplit(link).query)
                self.assertEqual(values["job_id"], [str(job_id)])
                self.assertEqual(len(values["page"]), 1)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(Path("."), "sqlite://", "/parity", "")

    def test_wb_uniform_and_size_conflict(self):
        price = WildberriesAdapter.normalize_price({"currencyIsoCode4217":"RUB", "sizes":[{"price":1000,"discountedPrice":800,"clubDiscountedPrice":760}]})
        self.assertEqual((price.base_price, price.discount_price, price.loyalty_price), (Decimal("1000"),Decimal("800"),Decimal("760")))
        conflict = WildberriesAdapter.normalize_price({"sizes":[{"price":1000},{"price":1200}]})
        self.assertIsNone(conflict.base_price)
        self.assertEqual(conflict.reasons["base_price"], "size_prices_differ")

    def test_ozon_price_semantics_no_reference_or_loyalty_substitution(self):
        result = OzonAdapter.normalize_price({"price":{"price":"1000","old_price":"9999","marketing_seller_price":"850","premium_price":"500","currency_code":"RUB"}})
        self.assertEqual(result.base_price, Decimal("1000"))
        self.assertEqual(result.discount_price, Decimal("850"))
        self.assertIsNone(result.loyalty_price)
        missing = OzonAdapter.normalize_price({"price":{"price":1000}})
        self.assertIsNone(missing.discount_price)

    def test_rate_limit_bounded_and_retry_after(self):
        calls, sleeps = [], []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, headers={"Retry-After":"2"}) if len(calls) == 1 else httpx.Response(200,json={"items":[],"cursor":""})
        client = OzonAdapter(self.settings,"123","private",transport=httpx.MockTransport(handler),sleep=sleeps.append)
        self.assertEqual(client.price_page()["items"], [])
        self.assertEqual(len(calls), 2)
        self.assertIn(2, sleeps)
        client.close()
        client = OzonAdapter(self.settings,"123","private",transport=httpx.MockTransport(lambda _:httpx.Response(429)),sleep=lambda _:None)
        with self.assertRaisesRegex(ApiError,"rate_limit"):
            client.price_page()
        client.close()

    def test_auth_error_is_not_retried_or_leaked(self):
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(403,json={"message":"private api key secret"})
        client=OzonAdapter(self.settings,"123","private",transport=httpx.MockTransport(handler),sleep=lambda _:None)
        with self.assertRaisesRegex(ApiError,"auth"):
            client.price_page()
        self.assertEqual(len(calls),1)
        client.close()

    def test_response_parsed_as_decimal_without_float(self):
        transport=httpx.MockTransport(lambda _:httpx.Response(200,text='{"items":[{"product_id":1,"price":{"price":1000.19}}],"cursor":""}'))
        client=OzonAdapter(self.settings,"123","private",transport=transport,sleep=lambda _:None)
        value=client.price_page()["items"][0]["price"]["price"]
        self.assertIsInstance(value,Decimal)
        self.assertEqual(value,Decimal("1000.19"))
        client.close()

    def test_wb_catalog_and_both_categories(self):
        calls=[]
        def handler(request):
            calls.append(request)
            if 'content-api' in request.url.host:
                self.assertEqual(json.loads(request.content)["settings"]["filter"]["withPhoto"],-1)
                return httpx.Response(200,json={"cards":[{"nmID":1,"vendorCode":"A","title":"name"}],"cursor":{"total":1}})
            return httpx.Response(200,json={"data":{"listGoods":[{"nmID":1,"currencyIsoCode4217":"RUB","sizes":[{"price":1000}]}]}})
        adapter=WildberriesAdapter(self.settings,'private',transport=httpx.MockTransport(handler),sleep=lambda _:None)
        self.assertEqual(list(adapter.products())[0].price.base_price,Decimal("1000"))
        self.assertEqual(len(calls),2)
        adapter.close()

    def test_large_marketplace_catalogs_use_real_pagination(self):
        wb_offsets, wb_cursors = [], []
        def wb_handler(request):
            if 'content-api' in request.url.host:
                cursor = json.loads(request.content)["settings"]["cursor"]
                wb_cursors.append(cursor)
                start = int(cursor.get("nmID", 0))
                stop = min(start + 100, 1001)
                return httpx.Response(200, json={"cards": [{"nmID": i, "vendorCode": str(i)} for i in range(start+1, stop+1)], "cursor": {"nmID": stop, "updatedAt": "2026-10-01", "total": stop-start}})
            start = int(request.url.params["offset"])
            wb_offsets.append(start)
            return httpx.Response(200, json={"data": {"listGoods": [{"nmID": i, "sizes": [{"price": 1000}], "currencyIsoCode4217": "RUB"} for i in range(start+1, min(start+1000, 1001)+1)]}})
        wb = WildberriesAdapter(self.settings, 'private', transport=httpx.MockTransport(wb_handler), sleep=lambda _: None)
        self.assertEqual(len(list(wb.products())), 1001)
        self.assertEqual(wb_offsets, [0, 1000])
        self.assertEqual(len(wb_cursors), 11)
        self.assertEqual(wb_cursors[-1]["nmID"], 1000)
        wb.close()

        ozon_cursors, ozon_last_ids = [], []
        def ozon_handler(request):
            body = json.loads(request.content)
            if request.url.path == '/v5/product/info/prices':
                ozon_cursors.append(body['cursor'])
                start = 1000 if body['cursor'] else 0
                return httpx.Response(200, json={"items": [{"product_id": i, "price": {"price": "1000", "currency_code": "RUB"}} for i in range(start+1, min(start+1000, 1001)+1)], "cursor": "next" if not start else ""})
            if request.url.path == '/v3/product/list':
                ozon_last_ids.append(body['last_id'])
                start = 1000 if body['last_id'] else 0
                return httpx.Response(200, json={"result": {"items": [{"product_id": i, "offer_id": str(i)} for i in range(start+1, min(start+1000, 1001)+1)], "last_id": "next"}})
            return httpx.Response(200, json={"items": [{"id": int(i), "offer_id": str(i), "sku": int(i)+10000} for i in body['product_id']]})
        ozon = OzonAdapter(self.settings, '123', 'private', transport=httpx.MockTransport(ozon_handler), sleep=lambda _: None)
        values = list(ozon.products())
        self.assertEqual(len(values), 1001)
        self.assertEqual(values[-1].sku, '11001')
        self.assertEqual(ozon_cursors, ['', 'next'])
        self.assertEqual(ozon_last_ids, ['', 'next'])
        ozon.close()

    def test_wb_rate_limit_header_is_respected(self):
        calls,sleeps=[],[]
        def handler(request):
            calls.append(request)
            return httpx.Response(429,headers={"X-Ratelimit-Retry":"7"}) if len(calls)==1 else httpx.Response(200,json={"data":{"listGoods":[]}})
        adapter=WildberriesAdapter(self.settings,'private',transport=httpx.MockTransport(handler),sleep=sleeps.append)
        self.assertEqual(adapter.price_page(),[])
        self.assertIn(7,sleeps)
        adapter.close()

    def test_long_cooldown_is_not_retried_early(self):
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(429,headers={"Retry-After":"120"})
        adapter=OzonAdapter(self.settings,'123','private',transport=httpx.MockTransport(handler),sleep=lambda _:None)
        with self.assertRaisesRegex(ApiError,'rate_limit'):
            adapter.price_page()
        self.assertEqual(len(calls),1)
        adapter.close()

    def test_ozon_cursor_stall_and_missing_details_fail_closed(self):
        adapter=OzonAdapter(self.settings,'123','private',transport=httpx.MockTransport(lambda _:httpx.Response(200,json={"items":[],"cursor":"repeat"})),sleep=lambda _:None)
        with self.assertRaisesRegex(ApiError,'contract'):
            list(adapter.products())
        adapter.close()

    def test_invalid_money_is_not_normalized_to_zero(self):
        for value in (-1,'NaN','Infinity'):
            with self.assertRaisesRegex(ApiError,'contract'):
                OzonAdapter.normalize_price({'price':{'price':value}})

    def test_ozon_real_endpoints_identifiers_and_cursor(self):
        requests=[]
        def handler(request):
            requests.append(request)
            body=json.loads(request.content)
            if request.url.path == '/v5/product/info/prices':
                if not body['cursor']:
                    return httpx.Response(200,json={"items":[{"product_id":1,"price":{"price":1000,"marketing_seller_price":900,"currency_code":"RUB"}}],"cursor":"next"})
                return httpx.Response(200,json={"items":[],"cursor":""})
            if request.url.path == '/v3/product/list':
                return httpx.Response(200,json={"result":{"items":[{"product_id":1,"offer_id":"A"}],"last_id":"last"}})
            self.assertEqual(request.url.path,'/v3/product/info/list')
            return httpx.Response(200,json={"items":[{"id":1,"sku":999,"offer_id":"A","name":"name"}]})
        adapter=OzonAdapter(self.settings,'123','private',transport=httpx.MockTransport(handler),sleep=lambda _:None)
        value=list(adapter.products())[0]
        self.assertEqual((value.external_id,value.sku,value.seller_article),('1','999','A'))
        self.assertEqual(value.price.discount_price,Decimal('900'))
        self.assertEqual(len(requests),4)
        adapter.close()


if __name__ == '__main__':
    unittest.main()
