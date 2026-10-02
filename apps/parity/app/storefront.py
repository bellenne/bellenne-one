"""Rendered buyer pages only. No seller prices, inferred discounts or challenge bypass.

DOM contract inspected on WB / Ozon in Moscow, 2026-10-01. Selectors use
semantic class prefixes / data-widget, never generated class suffixes.
"""
from dataclasses import dataclass
from pathlib import Path
from decimal import Decimal
import re
import time
from .domain import NormalizedPrice


ERRORS = {
    "blocked": "Сайт ограничил автоматический просмотр. Цена не обновлена. Откройте карточку на сайте; повторный сбор возможен после снятия ограничения.",
    "region": "На странице не подтверждён регион Москва. Цена не сохранена. Проверьте регион доставки на сайте.",
    "identity": "Артикул открытой карточки не совпадает с товаром. Проверьте покупательский SKU и ссылку в каталоге.",
    "benefit_missing": "Страница не подтвердила цену с WB Кошельком / Ozon Картой. Обычная цена не используется вместо неё.",
    "layout": "Не удалось однозначно прочитать блок цены. Структура страницы могла измениться; требуется обновление сборщика.",
    "unavailable": "Карточка недоступна или товар отсутствует в продаже. Проверьте ссылку на сайте.",
    "navigation": "Страница не загрузилась за отведённое время. Проверьте доступность сайта и повторите сбор позже.",
    "browser": "Браузер сборщика недоступен. Проверьте запуск parity-worker с Chromium.",
    "no_products": "В каталоге нет товара для проверки. Сначала импортируйте артикулы из кабинета продавца.",
    "internal": "Сбор не завершён. Откройте журнал задания для диагностики.",
    "interrupted": "Worker прервал сбор. Предыдущая фиксация сохранена; повторите сбор карточки.",
}


class CollectionError(Exception):
    def __init__(self, code, details=None):
        self.code, self.details = code, details or {}
        super().__init__(code)


def product_url(marketplace, external_id, sku):
    identifier = external_id if marketplace == "wb" else sku
    if marketplace not in ("wb", "ozon") or not identifier or not re.fullmatch(r"[1-9][0-9]{0,19}", str(identifier)):
        raise CollectionError("identity")
    return (f"https://www.wildberries.ru/catalog/{identifier}/detail.aspx" if marketplace == "wb"
            else f"https://www.ozon.ru/product/{identifier}/")


def rubles(value):
    if value is None:
        return None
    # Only one whole, labelled RUB value; never minimum of several prices.
    compact = re.sub(r"[\s\u00a0\u2009\u202f]", "", value)
    if not re.fullmatch(r"[0-9]+(?:[.,][0-9]{1,2})?₽", compact):
        raise CollectionError("layout")
    number = Decimal(compact[:-1].replace(",", "."))
    if number <= 0 or number > Decimal("1000000000"):
        raise CollectionError("layout")
    return number


# All observation comes from visible rendered DOM, scoped to the main buy box.
EXTRACT_SCRIPT = (Path(__file__).parent / "static" / "storefront-extract.js").read_text(encoding="utf-8")
EXTRACT = EXTRACT_SCRIPT.removeprefix("globalThis.parityExtract = ").rstrip().removesuffix(";")


@dataclass
class Observation:
    price: NormalizedPrice
    page_url: str
    context: dict
    evidence: bytes | None = None


def normalize_observation(data, marketplace, identifier, city, url, *, check_city=True):
    if data.get("blocked"):
        raise CollectionError("blocked")
    if data.get("unavailable"):
        raise CollectionError("unavailable")
    if data.get("identity") != str(identifier):
        raise CollectionError("identity")
    if check_city and data.get("city") != city:
        raise CollectionError("region")
    if not data.get("hasBox"):
        raise CollectionError("layout")
    if not data.get("benefitConfirmed") or not data.get("benefit"):
        raise CollectionError("benefit_missing")
    price = NormalizedPrice(base_price=rubles(data.get("reference")), discount_price=rubles(data.get("regular")),
                            loyalty_price=rubles(data["benefit"]), currency="RUB",
                            sources={"base_price": "storefront.reference", "discount_price": "storefront.regular",
                                     "loyalty_price": "storefront.wb_wallet" if marketplace == "wb" else "storefront.ozon_card"})
    for key in ("base_price", "discount_price"):
        if getattr(price, key) is None:
            price.reasons[key] = "not_visible"
    context = {"city": city if check_city else data.get("city"),
               "payment": "wb_wallet" if marketplace == "wb" else "ozon_card",
               "session": "guest", "currency": "RUB", "parser_version": 1}
    if not check_city:
        context["region_checked"] = False
    return Observation(price, url, context)


class StorefrontCollector:
    def __init__(self, settings, policy, heartbeat=lambda: None):
        self.settings, self.policy, self.heartbeat = settings, policy, heartbeat
        self.runtime = self.browser = None
        self.contexts = {}
        self.last_page = 0

    def close(self):
        if self.browser:
            self.browser.close()
        if self.runtime:
            self.runtime.stop()

    def collect(self, product):
        from playwright.sync_api import sync_playwright, Error, TimeoutError
        url = product_url(product.marketplace, product.external_id, product.sku)
        try:
            if self.browser is None:
                self.runtime = sync_playwright().start()
                self.browser = self.runtime.chromium.launch(headless=True)
            if product.marketplace not in self.contexts:
                context = self.browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow", accept_downloads=False)
                self.contexts[product.marketplace] = (context, context.new_page())
            _, page = self.contexts[product.marketplace]
            wait = self.policy.page_interval_seconds - (time.monotonic() - self.last_page)
            while wait > 0:
                self.heartbeat()
                time.sleep(min(wait, 1))
                wait -= 1
            self.last_page = time.monotonic()
            self.heartbeat()
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.settings.http_timeout * 1000)
            # Some storefronts initially return a browser-check page and then
            # navigate normally. Allow only the normal page flow within timeout.
            deadline = time.monotonic() + self.settings.http_timeout
            data = {}
            while time.monotonic() < deadline:
                self.heartbeat()
                data = page.evaluate(EXTRACT, {"marketplace": product.marketplace})
                if data.get("unavailable") or (data.get("identity") and data.get("hasBox")):
                    break
                page.wait_for_timeout(500)
            if data.get("blocked") or (response and response.status in (403, 429)):
                raise CollectionError("blocked", {"http_status": response.status if response else None})
            if response and response.status == 404:
                raise CollectionError("unavailable")
            identifier = product.external_id if product.marketplace == "wb" else product.sku
            result = normalize_observation(data, product.marketplace, identifier, self.policy.city, url)
            selector = '[class*="priceBlockPriceWrap--"]' if product.marketplace == "wb" else '[data-widget="webPrice"]'
            try:
                result.evidence = page.locator(selector).filter(visible=True).screenshot(type="jpeg", quality=80, timeout=5000)
            except Error:
                pass  # Price remains supported by the structured DOM observation.
            return result
        except CollectionError:
            raise
        except TimeoutError:
            raise CollectionError("navigation", {"browser_error": "timeout"}) from None
        except Error as exc:
            # Only a whitelisted network error code; never exception text / URLs,
            # headers, response HTML or browser storage in the journal.
            network = re.search(r"net::(ERR_[A-Z0-9_]+)", str(exc))
            raise CollectionError("browser" if self.browser is None else "navigation",
                                  {"browser_error": network.group(1) if network else "browser_operation"}) from None
