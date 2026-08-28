from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from datetime import date, timedelta
from typing import Any

import httpx

from app.config import Settings


class WBApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def chunks(items: list[int], size: int) -> Iterable[list[int]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def date_chunks(begin: date, end: date, days: int = 31):
    cursor = begin
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=days - 1), end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


class WildberriesClient:
    def __init__(
        self,
        token: str,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.wb_base_url,
            headers={
                "Authorization": token,
                "Accept": "application/json",
                "User-Agent": "wb-ads-statistics/1.0",
            },
            timeout=settings.wb_http_timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> "WildberriesClient":
        return self

    async def __aexit__(self, *_args) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        retries: int = 3,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= retries:
                    break
                await asyncio.sleep(min(2 ** attempt, 8))
                continue

            if response.status_code < 400:
                return response

            if response.status_code == 429 and attempt < retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                await asyncio.sleep(max(0.2, min(delay, 60)))
                continue

            if response.status_code >= 500 and attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue

            detail = response.text.strip().replace("\n", " ")[:400]
            raise WBApiError(
                f"Wildberries API вернул HTTP {response.status_code}: {detail}",
                response.status_code,
            )

        raise WBApiError(f"Ошибка соединения с Wildberries API: {last_error}")

    async def ping(self) -> bool:
        response = await self._get("/ping", retries=1)
        return response.status_code == 200

    async def list_campaigns(self) -> list[dict[str, Any]]:
        response = await self._get("/adv/v1/promotion/count")
        payload = response.json()
        groups = payload.get("adverts", []) if isinstance(payload, dict) else payload
        if isinstance(groups, dict):
            groups = [groups]

        campaigns: list[dict[str, Any]] = []
        for group in groups or []:
            status = group.get("status", group.get("statusId"))
            campaign_type = group.get("type")
            items = group.get("advert_list") or group.get("advertList") or []
            if not items and "id" in group:
                items = [group]
            for item in items:
                advert_id = item.get("advertId", item.get("id"))
                if advert_id is None:
                    continue
                campaigns.append(
                    {
                        "advert_id": int(advert_id),
                        "status": int(status) if status is not None else None,
                        "campaign_type": (
                            int(campaign_type)
                            if campaign_type is not None
                            else None
                        ),
                        "change_time": item.get("changeTime") or item.get("date"),
                    }
                )
        return campaigns

    async def campaign_details(
        self,
        campaign_ids: list[int],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        batches = list(chunks(campaign_ids, 50))
        for index, batch in enumerate(batches):
            response = await self._get(
                "/api/advert/v2/adverts",
                params={"ids": ",".join(str(item) for item in batch)},
            )
            payload = response.json()
            adverts = payload.get("adverts", []) if isinstance(payload, dict) else payload
            results.extend(adverts or [])
            if index < len(batches) - 1:
                await asyncio.sleep(
                    self.settings.wb_info_request_interval_seconds
                )
        return results

    async def full_stats(
        self,
        campaign_ids: list[int],
        begin: date,
        end: date,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        requests: list[tuple[list[int], date, date]] = []
        for chunk_begin, chunk_end in date_chunks(begin, end):
            for id_batch in chunks(campaign_ids, 50):
                requests.append((id_batch, chunk_begin, chunk_end))

        for index, (id_batch, chunk_begin, chunk_end) in enumerate(requests):
            response = await self._get(
                "/adv/v3/fullstats",
                params={
                    "ids": ",".join(str(item) for item in id_batch),
                    "beginDate": chunk_begin.isoformat(),
                    "endDate": chunk_end.isoformat(),
                },
            )
            payload = response.json()
            if not isinstance(payload, list):
                raise WBApiError(
                    "Wildberries API вернул статистику в неизвестном формате"
                )
            yield payload
            if index < len(requests) - 1:
                await asyncio.sleep(
                    self.settings.wb_request_interval_seconds
                )

