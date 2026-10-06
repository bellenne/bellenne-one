from datetime import timedelta
from io import BytesIO
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from sqlalchemy import select

from app.domain import now
from app.models import Policy, Product, Snapshot
from app.queries import catalog
from app.reporting import comparison_summary
from app.service import recalculate
from tests import test_parity as fixtures, test_storefront as storefront_fixtures
from tests.test_parity import product

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def worksheet(content):
    with ZipFile(BytesIO(content)) as archive:
        strings = ["".join(node.itertext()) for node in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", NS)]
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        rows = []
        for row in root.findall("s:sheetData/s:row", NS):
            values = {}
            for cell in row.findall("s:c", NS):
                v = cell.find("s:v", NS)
                if v is not None:
                    values[cell.attrib["r"]] = strings[int(v.text)] if cell.attrib.get("t") == "s" else float(v.text)
                if cell.find("s:f", NS) is not None:
                    raise AssertionError("User input became an Excel formula")
            rows.append(values)
        return root, rows


class ReportingTests(unittest.TestCase):
    setUp = fixtures.ParityTests.setUp
    tearDown = fixtures.ParityTests.tearDown
    sync = fixtures.ParityTests.sync

    def export(self, path="/products/export.xlsx"):
        response = self.client.get(path, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml.sheet", response.headers["content-type"])
        self.assertIn("attachment; filename=", response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        return worksheet(response.content)

    def test_export_all_pages_and_preserve_text_numbers_and_input(self):
        self.sync([product(i, f"ART-{i:03}", value="2917.25") for i in range(65)],
                  [product(i+100, f"ART-{i:03}", value="3043.50") for i in range(65)])
        with self.factory() as session:
            item = session.scalar(select(Product).where(Product.marketplace == "ozon", Product.seller_article == "ART-000"))
            item.sku = "00012345678901234567890"
            item.name = '=HYPERLINK("https://example.invalid", "x")'
            session.commit()
        root, rows = self.export("/products/export.xlsx?sort=article&page=2&page_size=1")
        self.assertEqual(len(rows), 66)
        self.assertEqual(rows[1]["F2"], "00012345678901234567890")
        self.assertTrue(rows[1]["D2"].startswith("=HYPERLINK"))
        self.assertEqual((rows[1]["P2"], rows[1]["Q2"], rows[1]["R2"]), (2917.25, 3043.50, 126.25))
        self.assertAlmostEqual(rows[1]["S2"], 126.25/2917.25)
        self.assertEqual(root.find("s:autoFilter", NS).attrib["ref"], "A1:AB66")
        self.assertEqual(root.find("s:sheetViews/s:sheetView/s:pane", NS).attrib["ySplit"], "1")

    def test_export_and_summary_share_search_direction_and_discrepancy_filters(self):
        self.sync([product(1, "FO-A"), product(2, "FO-B"), product(3, "FO-EQUAL")],
                  [product(101, "FO-A", value="1100"), product(102, "FO-B", value="900"), product(103, "FO-EQUAL")])
        _, rows = self.export("/discrepancies/export.xlsx?direction=ozon_cheaper&price_type=discount_price&min_percent=5")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["A2"], "FO-B")
        self.assertEqual(rows[1]["R2"], -100)
        _, rows = self.export("/products/export.xlsx?q=FO-A&exact=true")
        self.assertEqual(len(rows), 2)
        html = self.client.get("/discrepancies?direction=ozon_cheaper&price_type=discount_price&min_percent=5", headers=self.headers).text
        self.assertIn('data-comparison-count="ozon_cheaper">1', html)
        self.assertIn("direction=ozon_cheaper", html)
        self.assertNotIn("FO-A", html)

    def test_stale_missing_and_mixed_currency_do_not_produce_false_deltas(self):
        self.sync([product(1, "OLD"), product(2, "NO-PRICE", value=None), product(3, "CURRENCY")],
                  [product(101, "OLD", value="1100"), product(102, "NO-PRICE"), product(103, "CURRENCY", currency="USD")])
        with self.factory() as session:
            session.scalar(select(Product).where(Product.marketplace == "wb", Product.seller_article == "OLD")).updated_at = now()-timedelta(days=2)
            session.commit()
            summary = comparison_summary(catalog(session, 1, page_size=None)["rows"], "discount_price")
        self.assertEqual(summary["compared"], 0)
        self.assertEqual(summary["counts"]["unavailable"], 3)
        self.assertEqual(summary["top"], [])
        _, rows = self.export("/products/export.xlsx?sort=article")
        for row in rows[1:]:
            self.assertFalse(any(k.startswith(("R", "S", "N", "O", "V", "W")) for k in row))
        self.assertIn("Данные устарели", [r.get("H"+str(i)) for i,r in enumerate(rows[1:], 2)])
        self.assertIn(1000.0, [r.get("P"+str(i)) for i,r in enumerate(rows[1:], 2)])

    def test_empty_export_auth_and_owner_isolation(self):
        self.sync([product(1, "PRIVATE")], [product(2, "PRIVATE", value="1100")])
        self.assertEqual(self.client.get("/products/export.xlsx").status_code, 401)
        other = {**self.headers, "X-Bellenne-User-Id": "2"}
        self.client.get("/", headers=other)
        response = self.client.get("/products/export.xlsx", headers=other)
        self.assertEqual(len(worksheet(response.content)[1]), 1)
        self.assertNotIn("PRIVATE", self.client.get("/products", headers=other).text)
        self.assertEqual(len(self.export("/products/export.xlsx?q=ABSENT")[1]), 1)
        for query in ("min_percent=NaN", "price_type=fake", "direction=fake", "updated_since=invalid"):
            self.assertEqual(self.client.get("/products/export.xlsx?"+query, headers=self.headers).status_code, 422)

    def test_summary_counts_all_pages_and_ranks_actual_prices(self):
        self.sync([product(i, f"ART-{i:03}") for i in range(65)],
                  [product(i+100, f"ART-{i:03}", value=str(1000+i)) for i in range(65)])
        html = self.client.get("/products?page_size=1", headers=self.headers).text
        self.assertIn('data-comparison-count="wb_cheaper">64', html)
        self.assertIn('data-comparison-count="equal">1', html)
        self.assertIn("8 из 65 актуальных пар", html)
        with self.factory() as session:
            summary = comparison_summary(catalog(session,1,page_size=None)["rows"], "discount_price")
            self.assertEqual(summary["top"][0]["article"], "ART-064")
            self.assertEqual(summary["maximum"], 1064)

    def test_browser_charts_download_focus_and_responsive_layout(self):
        from playwright.sync_api import sync_playwright, expect
        self.sync([product(i, f"FO-{i:03}_300x270", value=str(3000+i*100), loyalty=str(2900+i*100)) for i in range(12)],
                  [product(i+100, f"FO-{i:03}_300x270", value=str(3000+i*100), loyalty=str(2900+i*100+(126 if i%3==0 else -230 if i%3==1 else 0))) for i in range(12)])
        with self.factory() as session:
            policy = session.get(Policy,1)
            policy.price_source = "storefront"
            for item in session.scalars(select(Product)):
                item.price_source = "storefront"
                item.price_context_version = policy.context_version
                item.collection_status = "success"
            for snapshot in session.scalars(select(Snapshot)):
                snapshot.source_kind = "storefront"
                snapshot.context_version = policy.context_version
            session.flush()
            recalculate(session,1)
            session.commit()

        def route_handler(route):
            url = urlsplit(route.request.url)
            # Local tests use the same shared assets copied by the Dockerfile.
            shared = {"/parity/static/theme.css": "css/theme.css", "/parity/static/app.css": "css/app.css", "/parity/static/app.js": "js/app.js"}
            source = Path(__file__).resolve().parents[2] / "pulse/app/static" / shared.get(url.path, "missing")
            if url.path in shared and source.is_file():
                route.fulfill(content_type="text/css" if url.path.endswith(".css") else "application/javascript", body=source.read_bytes())
                return
            response = self.client.get((url.path.removeprefix("/parity") or "/")+("?"+url.query if url.query else ""), headers=self.headers)
            route.fulfill(status=response.status_code, headers={k:v for k,v in response.headers.items() if k not in ("content-length","content-encoding")}, body=response.content)

        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            page.route("http://parity.test/**", route_handler)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto("http://parity.test/parity/products?page_size=5")
            expect(page.locator("svg.comparison-chart")).to_have_count(2)
            expect(page.locator('[data-comparison-count="wb_cheaper"]')).to_have_text("4")
            page.locator(".comparison-chart-link").first.focus()
            self.assertEqual(page.evaluate("document.activeElement.classList.contains('comparison-chart-link')"), True)
            page.locator(".comparison-more summary").first.click()
            expect(page.locator(".comparison-product details[open]").get_by_text("WB SKU:", exact=False)).to_be_visible()
            with page.expect_download() as downloaded:
                page.locator("[data-excel-export]").click()
            download = downloaded.value
            self.assertTrue(download.suggested_filename.endswith(".xlsx"))
            self.assertEqual(len(worksheet(Path(download.path()).read_bytes())[1]), 13)
            directory = os.getenv("PARITY_TEST_ARTIFACT_DIR")
            if directory:
                folder = Path(directory)
                folder.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(folder/"comparison-desktop.png"), full_page=True)
                download.save_as(str(folder/"comparison-preview.xlsx"))
            page.set_viewport_size({"width":390,"height":844})
            page.reload()
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
            expect(page.locator("[data-excel-export]")).to_be_visible()
            if directory:
                page.screenshot(path=str(Path(directory)/"comparison-mobile.png"), full_page=True)
            self.assertEqual(errors, [])
            browser.close()


class StorefrontReportingTests(unittest.TestCase):
    setUp = storefront_fixtures.StorefrontJobsTests.setUp
    tearDown = storefront_fixtures.StorefrontJobsTests.tearDown
    run_collection = storefront_fixtures.StorefrontJobsTests.run_collection

    def test_wallet_card_are_primary_and_other_prices_are_not_compared(self):
        self.run_collection()
        with self.factory() as session:
            summary = comparison_summary(catalog(session,1,page_size=None)["rows"], "loyalty_price")
            self.assertEqual((summary["top"][0]["wb"], summary["top"][0]["ozon"]), (2917, 3043))
        response = self.client.get("/products/export.xlsx", headers=self.headers)
        _, rows = worksheet(response.content)
        self.assertEqual((rows[1]["T2"], rows[1]["U2"], rows[1]["V2"]), (2917,3043,126))
        self.assertNotIn("R2", rows[1])
        self.assertNotIn("N2", rows[1])
        html = self.client.get("/products", headers=self.headers).text
        self.assertIn("Кошелёк / карта · разница", html)
        self.assertIn('data-comparison-count="wb_cheaper">1', html)
        self.run_collection(errors={"wb":"unavailable"})
        html = self.client.get("/products", headers=self.headers).text
        self.assertIn('data-comparison-count="unavailable">1', html)
        self.assertIn("Нет актуальной пары цен", html)
