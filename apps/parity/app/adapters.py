"""Read-only official API adapters. Field semantics: docs/PARITY_API.md.

WB: sizes.price / discountedPrice / clubDiscountedPrice (uniform sizes only).
Ozon: price.price (ceiling without promotions), marketing_seller_price (seller
promotion ceiling). old_price is a crossed-out reference, NOT the base price.
Ozon loyalty / card price is not exposed by this contract: always unavailable.
"""
import json
import time
from decimal import Decimal
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import httpx
from .domain import NormalizedPrice, NormalizedProduct, PRICE_TYPES, money


class ApiError(Exception):
    def __init__(self, code, http_status=None):
        self.code, self.http_status = code, http_status
        super().__init__(code)


ERRORS = {
    "auth": "API отклонил ключ или права доступа. Проверьте ключ и разрешения на товары и цены в кабинете маркетплейса.",
    "rate_limit": "Превышен лимит API. Повторите проверку или синхронизацию позже.",
    "temporary": "API временно недоступен. Сохранённые цены не изменены; повторите синхронизацию позже.",
    "contract": "Ответ API не соответствует поддерживаемому контракту. Требуется проверка адаптера; сохранённые данные не изменены.",
    "request": "API отклонил запрос. Проверьте доступность методов и настройки аккаунта.",
    "not_configured": "Подключение не настроено. Сохраните API-ключ на странице интеграций.",
    "credential_unreadable": "Не удалось расшифровать ключ. Восстановите ключ шифрования из резервной копии или сохраните API-ключ повторно.",
    "credentials_changed": "Ключ изменён во время задания. Запустите проверку и синхронизацию с новыми настройками.",
    "interrupted": "Worker прервал задание. Последние подтверждённые данные сохранены; запустите синхронизацию снова.",
    "internal": "Не удалось завершить задание. Откройте журнал с номером задания для диагностики.",
}


class ReadClient:
    def __init__(self, settings, headers, interval, *, transport=None, sleep=time.sleep, heartbeat=lambda: None):
        self.client = httpx.Client(headers=headers, timeout=settings.http_timeout, follow_redirects=False, transport=transport)
        self.attempts = settings.retry_attempts
        self.interval, self.sleep, self.heartbeat = interval, sleep, heartbeat
        self.last_request = 0

    def close(self):
        self.client.close()

    def request(self, method, url, **kwargs):
        for attempt in range(self.attempts):
            self.heartbeat()
            self.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            response = None
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.RequestError:
                code = "temporary"
            else:
                status = response.status_code
                if status in (401, 403):
                    raise ApiError("auth", status)
                if status == 429:
                    code = "rate_limit"
                elif status >= 500:
                    code = "temporary"
                elif status != 200:
                    raise ApiError("request", status)
                else:
                    try:
                        data = json.loads(response.text, parse_float=Decimal)
                    except ValueError:
                        raise ApiError("contract") from None
                    if not isinstance(data, dict) or data.get("error"):
                        raise ApiError("contract")
                    return data
            if attempt == self.attempts - 1:
                raise ApiError(code, response.status_code if response is not None else None)
            delay = 2 ** attempt
            for raw in ([response.headers.get("Retry-After"), response.headers.get("X-Ratelimit-Retry")] if response is not None else []):
                if not raw:
                    continue
                try:
                    delay = max(delay, float(raw))
                except ValueError:
                    try:
                        delay = max(delay, (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError):
                        pass
            # A long server cooldown must not cause a blind early retry or hold a
            # worker indefinitely. User can enqueue another job after cooldown.
            if delay > 60:
                raise ApiError(code, response.status_code if response is not None else None)
            self.sleep(delay)


def rows(data, key):
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ApiError("contract")
    return value


def identifier(data, key):
    value = data.get(key)
    if value is None or isinstance(value, bool) or not str(value):
        raise ApiError("contract")
    return str(value)


def safe_money(value):
    try:
        return money(value)
    except ValueError:
        raise ApiError("contract") from None


class WildberriesAdapter(ReadClient):
    CONTENT = "https://content-api.wildberries.ru/content/v2/get/cards/list"
    PRICES = "https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter"

    def __init__(self, settings, token, **kwargs):
        super().__init__(settings, {"Authorization": token}, .6, **kwargs)

    def card_page(self, cursor):
        data = self.request("POST", self.CONTENT, json={"settings": {"sort": {"ascending": True}, "cursor": cursor, "filter": {"withPhoto": -1}}})
        rows(data, "cards")
        if not isinstance(data.get("cursor"), dict):
            raise ApiError("contract")
        return data

    def price_page(self, offset=0, limit=1000):
        data = self.request("GET", self.PRICES, params={"limit": limit, "offset": offset})
        if not isinstance(data.get("data"), dict):
            raise ApiError("contract")
        return rows(data["data"], "listGoods")

    def probe(self):
        capabilities = {}
        for name, request in (("products", lambda: self.card_page({"limit": 1})), ("prices", lambda: self.price_page(limit=1))):
            try:
                request()
                capabilities[name] = {"status": "available"}
            except ApiError as exc:
                capabilities[name] = {"status": "unavailable", "error": exc.code, "http_status": exc.http_status}
        return capabilities

    @staticmethod
    def normalize_price(item):
        result = NormalizedPrice(currency=item.get("currencyIsoCode4217"))
        sizes = rows(item, "sizes")
        for key, source in zip(PRICE_TYPES, ("price", "discountedPrice", "clubDiscountedPrice")):
            values = [safe_money(size.get(source)) for size in sizes]
            uniform = bool(values) and None not in values and len(set(values)) == 1
            setattr(result, key, values[0] if uniform else None)
            result.sources[key] = f"sizes.{source}"
            if not uniform:
                result.reasons[key] = "size_prices_differ" if len(set(values)) > 1 else "not_provided"
        return result

    def products(self, include_prices=True):
        cards, cursor, cursors = {}, {"limit": 100}, set()
        while True:
            data = self.card_page(cursor)
            page = data["cards"]
            for card in page:
                external = identifier(card, "nmID")
                if external in cards:
                    raise ApiError("contract")
                cards[external] = card
            if len(page) < 100:
                break
            nxt = data["cursor"]
            signature = (nxt.get("updatedAt"), nxt.get("nmID"))
            if None in signature or signature in cursors:
                raise ApiError("contract")
            cursors.add(signature)
            cursor = {"limit": 100, "updatedAt": signature[0], "nmID": signature[1]}
        prices, offset, seen_pages = {}, 0, set()
        while include_prices:
            page = self.price_page(offset)
            signature = tuple(identifier(item, "nmID") for item in page)
            if signature and signature in seen_pages:
                raise ApiError("contract")
            seen_pages.add(signature)
            for item in page:
                external = identifier(item, "nmID")
                if external in prices:
                    raise ApiError("contract")
                prices[external] = self.normalize_price(item)
            if len(page) < 1000:
                break
            offset += len(page)
        for external, card in cards.items():
            yield NormalizedProduct(external, str(card.get("vendorCode") or ""), str(card.get("title") or ""), external, "active", prices.get(external, NormalizedPrice(reasons={key: "not_provided" for key in PRICE_TYPES})))


class OzonAdapter(ReadClient):
    BASE = "https://api-seller.ozon.ru"

    def __init__(self, settings, client_id, token, **kwargs):
        super().__init__(settings, {"Client-Id": client_id, "Api-Key": token}, .02, **kwargs)

    def list_page(self, last_id="", limit=1000):
        data = self.request("POST", self.BASE + "/v3/product/list", json={"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": limit})
        if not isinstance(data.get("result"), dict):
            raise ApiError("contract")
        rows(data["result"], "items")
        return data["result"]

    def price_page(self, cursor="", limit=1000):
        data = self.request("POST", self.BASE + "/v5/product/info/prices", json={"filter": {"visibility": "ALL"}, "cursor": cursor, "limit": limit})
        rows(data, "items")
        return data

    def probe(self):
        capabilities = {}
        def probe_products():
            page = self.list_page(limit=1)
            if page["items"]:
                external = identifier(page["items"][0], "product_id")
                details = rows(self.request("POST", self.BASE + "/v3/product/info/list", json={"product_id": [external]}), "items")
                if len(details) != 1 or identifier(details[0], "id") != external:
                    raise ApiError("contract")
            return bool(page["items"])

        for name, request in (("products", probe_products), ("prices", lambda: self.price_page(limit=1))):
            try:
                result = request()
                capabilities[name] = {"status": "available"}
                if name == "products":
                    capabilities[name]["details_checked"] = result
            except ApiError as exc:
                capabilities[name] = {"status": "unavailable", "error": exc.code, "http_status": exc.http_status}
        return capabilities

    @staticmethod
    def normalize_price(item):
        value = item.get("price")
        if not isinstance(value, dict):
            raise ApiError("contract")
        result = NormalizedPrice(
            base_price=safe_money(value.get("price")),
            discount_price=safe_money(value.get("marketing_seller_price")),
            currency=value.get("currency_code"),
            sources={"base_price": "price.price", "discount_price": "price.marketing_seller_price", "loyalty_price": "not_supported"},
            reasons={"loyalty_price": "not_supported"},
        )
        for key in PRICE_TYPES:
            if getattr(result, key) is None:
                result.reasons.setdefault(key, "not_provided")
        return result

    def products(self, include_prices=True):
        prices, cursor, seen = {}, "", set()
        while include_prices:
            page = self.price_page(cursor)
            for item in page["items"]:
                external = identifier(item, "product_id")
                if external in prices:
                    raise ApiError("contract")
                prices[external] = self.normalize_price(item)
            nxt = page.get("cursor")
            if not nxt:
                break
            if nxt in seen:
                raise ApiError("contract")
            seen.add(nxt)
            cursor = nxt
        last_id, seen, product_ids = "", set(), set()
        while True:
            page = self.list_page(last_id)
            items = page["items"]
            if not items:
                break
            ids = [identifier(item, "product_id") for item in items]
            if product_ids.intersection(ids) or len(set(ids)) != len(ids):
                raise ApiError("contract")
            product_ids.update(ids)
            details = rows(self.request("POST", self.BASE + "/v3/product/info/list", json={"product_id": ids}), "items")
            info = {identifier(item, "id"): item for item in details}
            if set(info) != set(ids):
                raise ApiError("contract")
            for item in items:
                external = str(item["product_id"])
                detail = info[external]
                yield NormalizedProduct(external, str(detail.get("offer_id") or item.get("offer_id") or ""), str(detail.get("name") or ""), str(detail["sku"]) if detail.get("sku") else None, "archived" if detail.get("is_archived") or detail.get("is_autoarchived") else str((detail.get("statuses") or {}).get("status") or "active"), prices.get(external, NormalizedPrice(reasons={key: "not_provided" for key in PRICE_TYPES})))
            if len(items) < 1000:
                break
            nxt = page.get("last_id")
            if not nxt or nxt in seen:
                raise ApiError("contract")
            seen.add(nxt)
            last_id = nxt
