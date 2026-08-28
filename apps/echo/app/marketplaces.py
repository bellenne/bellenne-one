import asyncio
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from .models import Cabinet


WB_QUESTIONS_REVIEWS_INTERVAL_SECONDS = 0.36
WB_RATE_LIMIT_RETRIES = 3
WB_FEEDBACK_PAGE_SIZE = 5000
WB_FEEDBACK_TAKE_LIMIT = 5000


class MarketplaceError(Exception):
    pass


@dataclass
class Review:
    id: str
    product_name: str = ""
    article: str = ""
    category: str = ""
    kind: str = "text"
    rating: int = 0
    rating_raw: str = ""
    rating_source: str = ""
    text: str = ""
    raw: dict | None = None
    info_loaded: bool = False


@dataclass
class ReviewFetchSnapshot:
    reviews: list[Review]
    diagnostics: str = ""


def _kind_from_media(has_photo: bool, has_video: bool) -> str:
    if has_video:
        return "video"
    if has_photo:
        return "photo"
    return "text"


class _RequestIntervalLimiter:
    def __init__(self, interval_seconds: float):
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            scheduled_at = now + delay
            self._next_at = scheduled_at + self.interval_seconds

        if delay:
            await asyncio.sleep(delay)


_wb_questions_reviews_limiter = _RequestIntervalLimiter(WB_QUESTIONS_REVIEWS_INTERVAL_SECONDS)


class WildberriesClient:
    base_url = "https://feedbacks-api.wildberries.ru"

    def __init__(self, cabinet: Cabinet):
        self.cabinet = cabinet
        self.headers = {"Authorization": cabinet.wb_api_key.strip()}

    async def fetch_reviews_to_answer(self, limit: int) -> list[Review]:
        return (await self.fetch_queue_snapshot(limit)).reviews

    async def fetch_unanswered(self, limit: int) -> list[Review]:
        return await self.fetch_reviews_to_answer(limit)

    async def fetch_review_by_id(self, review_id: str) -> Review | None:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await self._request(client, "GET", "/api/v1/feedback", params={"id": review_id})
        if response.status_code in {404, 405}:
            return None
        if response.status_code >= 400:
            raise MarketplaceError(_friendly_error(response.status_code, response.text))

        item = _single_feedback_payload(response.json())
        if not item:
            return None
        return self._review_from_item(item)

    async def fetch_queue_snapshot(self, take: int = WB_FEEDBACK_TAKE_LIMIT) -> ReviewFetchSnapshot:
        take = min(max(1, take), WB_FEEDBACK_TAKE_LIMIT)
        async with httpx.AsyncClient(timeout=25) as client:
            unanswered_items = await self._fetch_feedbacks(client, is_answered=False, limit=take)
            answered_items = await self._fetch_feedbacks(client, is_answered=True, limit=take)

        answered_with_answer = sum(1 for item in answered_items if _has_real_answer(item))
        answered_without_answer = [item for item in answered_items if not _has_real_answer(item)]
        candidate_items = unanswered_items + answered_without_answer
        diagnostics = (
            f"WB получено отзывов: isAnswered=false {len(unanswered_items)}; "
            f"isAnswered=true {len(answered_items)}, с answer {answered_with_answer}, "
            f"без answer {len(answered_without_answer)}; take {take}"
        )
        return ReviewFetchSnapshot(
            reviews=_unique_reviews([self._review_from_item(item) for item in candidate_items], len(candidate_items)),
            diagnostics=diagnostics,
        )

    async def _fetch_feedbacks(self, client: httpx.AsyncClient, is_answered: bool, limit: int) -> list[dict]:
        feedbacks: list[dict] = []
        skip = 0

        while len(feedbacks) < limit:
            take = min(WB_FEEDBACK_PAGE_SIZE, limit - len(feedbacks))
            params = {"isAnswered": str(is_answered).lower(), "take": take, "skip": skip, "order": "dateAsc"}
            response = await self._request(client, "GET", "/api/v1/feedbacks", params=params)
            if response.status_code >= 400:
                raise MarketplaceError(_friendly_error(response.status_code, response.text))

            page = response.json().get("data", {}).get("feedbacks", [])
            if not page:
                break

            feedbacks.extend(page)
            if len(page) < take:
                break
            skip += take

        return feedbacks

    def _review_from_item(self, item: dict) -> Review:
        details = item.get("productDetails") or {}
        rating_raw = item.get("productValuation")
        return Review(
            id=str(item.get("id", "")),
            product_name=details.get("productName") or "",
            article=str(details.get("supplierArticle") or details.get("nmId") or ""),
            category=item.get("subjectName") or "",
            kind=_kind_from_media(bool(item.get("photoLinks")), bool(item.get("video"))),
            rating=parse_rating(rating_raw),
            rating_raw=_stringify_raw_rating(rating_raw),
            rating_source="productValuation",
            text=" ".join(filter(None, [item.get("text"), item.get("pros"), item.get("cons")]))[:1000],
        )

    async def send_reply(self, review_id: str, text: str) -> None:
        payload = {"id": review_id, "text": text}
        async with httpx.AsyncClient(timeout=25) as client:
            response = await self._request(client, "POST", "/api/v1/feedbacks/answer", json=payload)
        if response.status_code not in {200, 201, 204}:
            raise MarketplaceError(_friendly_error(response.status_code, response.text))

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(WB_RATE_LIMIT_RETRIES + 1):
            await _wb_questions_reviews_limiter.wait()
            try:
                response = await client.request(method, f"{self.base_url}{path}", headers=self.headers, **kwargs)
            except httpx.RequestError as exc:
                raise MarketplaceError(_network_error(exc)) from exc

            if response.status_code != 429 or attempt >= WB_RATE_LIMIT_RETRIES:
                return response

            await asyncio.sleep(_retry_after_seconds(response) or WB_QUESTIONS_REVIEWS_INTERVAL_SECONDS * 3 * (attempt + 1))

        return response


class OzonClient:
    base_url = "https://api-seller.ozon.ru"

    def __init__(self, cabinet: Cabinet):
        self.cabinet = cabinet
        self.headers = {
            "Client-Id": cabinet.ozon_client_id.strip(),
            "Api-Key": cabinet.ozon_api_key.strip(),
            "Content-Type": "application/json",
        }

    async def fetch_reviews_to_answer(self, limit: int) -> list[Review]:
        raw_items = await self._fetch_review_list("UNPROCESSED", limit)
        processed_items = await self._fetch_review_list("PROCESSED", _candidate_limit(limit))
        reviews: list[Review] = []

        async with httpx.AsyncClient(timeout=25) as client:
            for raw in raw_items[:limit]:
                review_id = _review_id(raw)
                if review_id:
                    reviews.append(await self._load_review_info(client, review_id, raw))

            for raw in processed_items[:limit]:
                review_id = _review_id(raw)
                if not review_id:
                    continue
                review = await self._load_review_info(client, review_id, raw)
                if review.info_loaded and not _has_real_answer(review.raw or {}):
                    reviews.append(review)

        return _unique_reviews(reviews, limit)

    async def fetch_unanswered(self, limit: int) -> list[Review]:
        return await self.fetch_reviews_to_answer(limit)

    async def fetch_review_by_id(self, review_id: str) -> Review | None:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await self._post_first_available(client, ["/v2/review/info", "/v1/review/info"], {"review_id": review_id})
        if response.status_code in {404, 405}:
            return None
        if response.status_code >= 400:
            raise MarketplaceError(_friendly_error(response.status_code, response.text))

        data = response.json().get("result") or {}
        return self._review_from_data(review_id, data, info_loaded=True)

    def _review_from_data(self, review_id: str, data: dict, info_loaded: bool) -> Review:
        product = data.get("product") or {}
        photos = data.get("photos") or data.get("photo") or []
        videos = data.get("videos") or data.get("video") or []
        article = str(product.get("offer_id") or data.get("offer_id") or product.get("sku") or data.get("sku") or "")
        category = str(product.get("category_name") or data.get("category_name") or "")
        rating_source, rating_raw = _first_present(data, ("rating", "score"))
        return Review(
            id=review_id,
            product_name=str(product.get("name") or data.get("product_name") or data.get("sku_name") or ""),
            article=article,
            category=category,
            kind=_kind_from_media(bool(photos), bool(videos)),
            rating=parse_rating(rating_raw),
            rating_raw=_stringify_raw_rating(rating_raw),
            rating_source=rating_source,
            text=str(data.get("text") or data.get("review_text") or "")[:1000],
            raw=data,
            info_loaded=info_loaded,
        )

    async def _fetch_review_list(self, status: str, limit: int) -> list[dict]:
        payload = {"status": status, "limit": limit, "offset": 0, "sort_dir": "ASC"}
        async with httpx.AsyncClient(timeout=25) as client:
            response = await self._post_first_available(client, ["/v2/review/list", "/v1/review/list"], payload)
        if response.status_code >= 400:
            raise MarketplaceError(_friendly_error(response.status_code, response.text))

        result = response.json().get("result") or {}
        return result.get("reviews") or result.get("items") or []

    async def _load_review_info(self, client: httpx.AsyncClient, review_id: str, fallback: dict) -> Review:
        response = await self._post_first_available(client, ["/v2/review/info", "/v1/review/info"], {"review_id": review_id})
        data = fallback
        info_loaded = False
        if response.status_code < 400:
            data = response.json().get("result") or fallback
            info_loaded = True

        return self._review_from_data(review_id, data, info_loaded)

    async def send_reply(self, review_id: str, text: str) -> None:
        payload = {"review_id": review_id, "text": text}
        async with httpx.AsyncClient(timeout=25) as client:
            try:
                response = await client.post(f"{self.base_url}/v1/review/comment/create", json=payload, headers=self.headers)
            except httpx.RequestError as exc:
                raise MarketplaceError(_network_error(exc)) from exc
        if response.status_code not in {200, 201, 204}:
            raise MarketplaceError(_friendly_error(response.status_code, response.text))

    async def _post_first_available(self, client: httpx.AsyncClient, paths: list[str], payload: dict) -> httpx.Response:
        last_response: httpx.Response | None = None
        for path in paths:
            try:
                response = await client.post(f"{self.base_url}{path}", json=payload, headers=self.headers)
            except httpx.RequestError as exc:
                raise MarketplaceError(_network_error(exc)) from exc
            last_response = response
            if response.status_code not in {404, 405}:
                return response
        return last_response


def client_for(cabinet: Cabinet) -> WildberriesClient | OzonClient:
    if cabinet.marketplace == "wb":
        return WildberriesClient(cabinet)
    return OzonClient(cabinet)


def _review_id(raw: dict) -> str:
    return str(raw.get("id") or raw.get("review_id") or "")


def _unique_reviews(reviews: list[Review], limit: int) -> list[Review]:
    result: list[Review] = []
    seen: set[str] = set()
    for review in reviews:
        if not review.id or review.id in seen:
            continue
        seen.add(review.id)
        result.append(review)
        if len(result) >= limit:
            break
    return result


def _candidate_limit(limit: int) -> int:
    return min(max(limit * 3, limit), 100)


def parse_rating(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
        if not value:
            return 0
        if "e" in value.lower():
            return 0
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return 0
    if not numeric.is_finite() or numeric != numeric.to_integral_value():
        return 0
    rating = int(numeric)
    return rating if 1 <= rating <= 5 else 0


def _first_present(data: dict, keys: tuple[str, ...]) -> tuple[str, object]:
    for key in keys:
        if key in data:
            return key, data.get(key)
    return "", None


def _stringify_raw_rating(value: object) -> str:
    if value is None:
        return ""
    return str(value)[:80]


def _single_feedback_payload(payload: dict) -> dict | None:
    data = payload.get("data")
    if isinstance(data, dict):
        if isinstance(data.get("feedback"), dict):
            return data["feedback"]
        if isinstance(data.get("feedbacks"), list):
            for item in data["feedbacks"]:
                if isinstance(item, dict):
                    return item
        return data
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                return item

    if isinstance(payload.get("feedback"), dict):
        return payload["feedback"]
    if isinstance(payload.get("feedbacks"), list):
        for item in payload["feedbacks"]:
            if isinstance(item, dict):
                return item
    return None


def _wb_candidate_limit(limit: int) -> int:
    return min(max(limit * 5, 50), WB_FEEDBACK_TAKE_LIMIT)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _has_real_answer(item: dict) -> bool:
    answer_fields = {
        "answer",
        "answers",
        "reply",
        "replies",
        "response",
        "responses",
        "comment",
        "comments",
        "seller_comment",
        "seller_comments",
        "brand_comment",
        "brand_comments",
        "company_answer",
        "company_answers",
        "official_comment",
        "official_comments",
        "published_comment",
        "published_comments",
    }
    for key, value in item.items():
        normalized_key = key.lower()
        if normalized_key in answer_fields or _looks_like_answer_content_field(normalized_key):
            if _answer_value_has_text(value):
                return True
    return False


def _looks_like_answer_content_field(key: str) -> bool:
    suffixes = (
        "_answer",
        "_answers",
        "_reply",
        "_replies",
        "_response",
        "_responses",
        "_comment",
        "_comments",
    )
    return key.endswith(suffixes)


def _answer_value_has_text(value: object) -> bool:
    answer_text_fields = {
        "answer",
        "answers",
        "reply",
        "replies",
        "response",
        "responses",
        "comment",
        "comments",
        "seller_comment",
        "seller_comments",
        "brand_comment",
        "brand_comments",
        "company_answer",
        "company_answers",
        "official_comment",
        "official_comments",
        "published_comment",
        "published_comments",
        "text",
    }
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(
            _answer_value_has_text(child)
            for key, child in value.items()
            if key.lower() in answer_text_fields or _looks_like_answer_content_field(key.lower())
        )
    if isinstance(value, list):
        return any(_answer_value_has_text(child) for child in value)
    return False


def _friendly_error(status_code: int, text: str) -> str:
    if status_code in {401, 403}:
        return "Кабинет не принял ключи доступа"
    if status_code == 429:
        return "Маркетплейс попросил попробовать позже"
    if status_code >= 500:
        return "Маркетплейс временно не отвечает"
    return (text or "Не удалось выполнить запрос")[:500]


def _network_error(exc: httpx.RequestError) -> str:
    return f"Не удалось подключиться к маркетплейсу: {exc}"
