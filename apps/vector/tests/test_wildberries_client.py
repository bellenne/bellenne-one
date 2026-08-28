from datetime import date
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.wildberries import WBApiError, WildberriesClient


@pytest.mark.asyncio
async def test_client_parses_campaigns_and_splits_api_limits(settings):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/adv/v1/promotion/count":
            return httpx.Response(
                200,
                json={
                    "adverts": [
                        {
                            "type": 8,
                            "status": 9,
                            "advert_list": [
                                {
                                    "advertId": 12345,
                                    "changeTime": "2026-07-27T10:00:00Z",
                                }
                            ],
                        }
                    ],
                    "all": 1,
                },
            )
        if request.url.path == "/api/advert/v2/adverts":
            return httpx.Response(200, json={"adverts": []})
        if request.url.path == "/adv/v3/fullstats":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with WildberriesClient(
        "test-secret-token",
        settings,
        transport=transport,
    ) as client:
        campaigns = await client.list_campaigns()
        assert campaigns == [
            {
                "advert_id": 12345,
                "status": 9,
                "campaign_type": 8,
                "change_time": "2026-07-27T10:00:00Z",
            }
        ]

        campaign_ids = list(range(1, 52))
        await client.campaign_details(campaign_ids)
        batches = [
            payload
            async for payload in client.full_stats(
                campaign_ids,
                date(2026, 6, 30),
                date(2026, 7, 31),
            )
        ]
        assert batches == [[], [], [], []]

    detail_requests = [
        item for item in requests
        if item.url.path == "/api/advert/v2/adverts"
    ]
    stat_requests = [
        item for item in requests
        if item.url.path == "/adv/v3/fullstats"
    ]
    assert len(detail_requests) == 2
    assert len(stat_requests) == 4
    assert all(
        len(item.url.params["ids"].split(",")) <= 50
        for item in detail_requests + stat_requests
    )
    assert {
        (item.url.params["beginDate"], item.url.params["endDate"])
        for item in stat_requests
    } == {
        ("2026-06-30", "2026-07-30"),
        ("2026-07-31", "2026-07-31"),
    }
    assert all(
        item.headers["Authorization"] == "test-secret-token"
        for item in requests
    )


@pytest.mark.asyncio
async def test_client_retries_rate_limit_and_reports_unauthorized(
    settings,
    monkeypatch,
):
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        if request.url.path == "/adv/v1/promotion/count":
            request_count += 1
            if request_count == 1:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "0"},
                    text="rate limited",
                )
            return httpx.Response(200, json={"adverts": [], "all": 0})
        if request.url.path == "/ping":
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(404)

    sleep = AsyncMock()
    monkeypatch.setattr("app.services.wildberries.asyncio.sleep", sleep)

    async with WildberriesClient(
        "test-secret-token",
        settings,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert await client.list_campaigns() == []
        sleep.assert_awaited_once_with(0.2)

        with pytest.raises(WBApiError, match="HTTP 401") as caught:
            await client.ping()

    assert caught.value.status_code == 401
    assert request_count == 2
