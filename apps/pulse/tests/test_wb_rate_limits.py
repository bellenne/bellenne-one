from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import httpx

from app.services.integrations import base as integration_base
from app.services.integrations import wb as wb_module
from app.services.integrations.wb import WildberriesIntegration


def test_request_json_honours_wb_retry_header(monkeypatch):
    responses = iter(
        [
            httpx.Response(429, headers={"X-Ratelimit-Retry": "7"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float):
        sleeps.append(seconds)

    async def run_request():
        async def handler(request: httpx.Request) -> httpx.Response:
            response = next(responses)
            response.request = request
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await integration_base.request_json(client, "GET", "https://example.test", attempts=2)

    monkeypatch.setattr(integration_base.asyncio, "sleep", fake_sleep)
    assert asyncio.run(run_request()) == {"ok": True}
    assert sleeps == [7.0]


def test_wb_advert_stats_are_batched_and_paced(monkeypatch):
    stats_calls: list[list[str]] = []
    sleeps: list[float] = []

    async def fake_sleep(seconds: float):
        sleeps.append(seconds)

    async def fake_request_json(client, method, url, **kwargs):
        if url.endswith("/adv/v1/upd"):
            return []
        if url.endswith("/adv/v1/promotion/count"):
            return {
                "adverts": [
                    {
                        "status": 9,
                        "advert_list": [{"advertId": value} for value in range(1, 102)],
                    },
                    {"status": 4, "advert_list": [{"advertId": 999}]},
                ]
            }
        ids = kwargs["params"]["ids"].split(",")
        stats_calls.append(ids)
        assert kwargs["attempts"] == 5
        return []

    monkeypatch.setattr(wb_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(wb_module, "request_json", fake_request_json)
    integration = WildberriesIntegration({"api_token": "token"})
    asyncio.run(integration._attach_advertising({}, date(2026, 8, 25)))

    assert [len(batch) for batch in stats_calls] == [50, 50, 1]
    assert all("999" not in batch for batch in stats_calls)
    assert sleeps == [20.0, 20.0]


def test_wb_daily_summary_uses_exact_date_summary_prices_and_safe_return_signs(monkeypatch):
    request_params: dict[str, dict] = {}

    async def fake_request_json(client, method, url, **kwargs):
        if url.endswith("/supplier/orders"):
            request_params["orders"] = kwargs["params"]
            return [
                {
                    "date": "2026-08-25T10:00:00",
                    "srid": "order-1",
                    "nmId": 1001,
                    "supplierArticle": "SKU-1",
                    "subject": "Фотообои",
                    "priceWithDisc": 1500,
                    "finishedPrice": 1200,
                    "totalPrice": 2000,
                    "isCancel": False,
                },
                {
                    "date": "2026-08-25T11:00:00",
                    "srid": "order-2",
                    "nmId": 1001,
                    "supplierArticle": "SKU-1",
                    "subject": "Фотообои",
                    "priceWithDisc": 900,
                    "finishedPrice": 700,
                    "totalPrice": 1200,
                    "isCancel": True,
                },
                {
                    "date": "2026-08-24T23:59:59",
                    "srid": "another-day",
                    "nmId": 1001,
                    "priceWithDisc": 99999,
                },
            ]
        if url.endswith("/supplier/sales"):
            request_params["sales"] = kwargs["params"]
            return [
                {
                    "date": "2026-08-25T12:00:00",
                    "saleID": "S-1",
                    "nmId": 1001,
                    "supplierArticle": "SKU-1",
                    "subject": "Фотообои",
                    "priceWithDisc": 1100,
                    "finishedPrice": 950,
                    "totalPrice": 1400,
                    "forPay": 700,
                },
                {
                    "date": "2026-08-25T13:00:00",
                    "saleID": "R-1",
                    "nmId": 1001,
                    "supplierArticle": "SKU-1",
                    "subject": "Фотообои",
                    "priceWithDisc": -400,
                    "finishedPrice": -350,
                    "totalPrice": -500,
                    "forPay": -250,
                },
                {
                    "date": "2026-08-25T14:00:00",
                    "saleID": "R-2",
                    "nmId": 1001,
                    "supplierArticle": "SKU-1",
                    "subject": "Фотообои",
                    "priceWithDisc": 100,
                    "finishedPrice": 80,
                    "totalPrice": 120,
                    "forPay": 50,
                },
            ]
        if url.endswith("/adv/v1/upd"):
            return []
        if url.endswith("/adv/v1/promotion/count"):
            return {"adverts": []}
        raise AssertionError(url)

    monkeypatch.setattr(wb_module, "request_json", fake_request_json)
    payload = asyncio.run(
        WildberriesIntegration({"api_token": "token"}).fetch(date(2026, 8, 25))
    )

    assert request_params == {
        "orders": {"dateFrom": "2026-08-25", "flag": 1},
        "sales": {"dateFrom": "2026-08-25", "flag": 1},
    }
    assert payload.total.ordered_units == 2
    assert payload.total.ordered_amount == Decimal("2400")
    assert payload.total.buyout_units == -1
    assert payload.total.buyout_amount == Decimal("600")
    assert payload.total.net_revenue == Decimal("400")
    assert payload.warnings == []


def test_wb_warns_instead_of_substituting_a_different_price_basis(monkeypatch):
    async def fake_request_json(client, method, url, **kwargs):
        if url.endswith("/supplier/orders"):
            return [
                {
                    "date": "2026-08-25T10:00:00",
                    "srid": "order-1",
                    "nmId": 1001,
                    "priceWithDisc": 0,
                    "finishedPrice": 1200,
                    "totalPrice": 2000,
                }
            ]
        if url.endswith("/supplier/sales"):
            return [
                {
                    "date": "2026-08-25T12:00:00",
                    "saleID": "S-1",
                    "nmId": 1001,
                    "priceWithDisc": 1000,
                    "totalPrice": 1200,
                    "forPay": 0,
                }
            ]
        if url.endswith("/adv/v1/upd"):
            return []
        if url.endswith("/adv/v1/promotion/count"):
            return {"adverts": []}
        raise AssertionError(url)

    monkeypatch.setattr(wb_module, "request_json", fake_request_json)
    payload = asyncio.run(
        WildberriesIntegration({"api_token": "token"}).fetch(date(2026, 8, 25))
    )

    assert payload.total.ordered_amount == Decimal("0")
    assert payload.warnings == [
        "WB ещё не заполнил priceWithDisc у 1 позиций. "
        "Суммы за день предварительные; повторите синхронизацию позже.",
        "WB ещё не заполнил forPay у 1 позиций. "
        "Значение «К перечислению за товар» предварительное; "
        "повторите синхронизацию позже.",
    ]


def test_wb_advert_costs_are_split_by_payment_source():
    integration = WildberriesIntegration({"api_token": "token"})
    source_totals = integration._source_totals(
        [
            {"advertId": 77, "updSum": 70, "paymentType": "Баланс"},
            {"advertId": 77, "updSum": 20, "paymentType": "Промо бонусы"},
            {"advertId": 77, "updSum": 30, "paymentType": "Кэшбэк"},
            {"advertId": 77, "updSum": 10, "paymentType": "Счёт"},
        ]
    )
    assert source_totals == {77: (Decimal("80"), Decimal("50"))}

    products = {}
    stats = [
        {
            "advertId": 77,
            "days": [
                {
                    "date": "2026-08-25T00:00:00+03:00",
                    "apps": [
                        {
                            "nms": [
                                {"nmId": 1001, "name": "Товар 1", "sum": 75},
                                {"nmId": 1002, "name": "Товар 2", "sum": 25},
                            ]
                        }
                    ],
                }
            ],
        }
    ]
    integration._apply_stats(products, stats, date(2026, 8, 25), source_totals)

    assert products["1001"].metrics.ad_spend == Decimal("60")
    assert products["1001"].metrics.ad_bonus_spend == Decimal("37.5")
    assert products["1002"].metrics.ad_spend == Decimal("20")
    assert products["1002"].metrics.ad_bonus_spend == Decimal("12.5")


def test_wb_cost_history_remains_authoritative_when_product_stats_fail(monkeypatch):
    async def fake_request_json(client, method, url, **kwargs):
        if url.endswith("/supplier/orders") or url.endswith("/supplier/sales"):
            return []
        if url.endswith("/adv/v1/upd"):
            return [
                {"advertId": 77, "updSum": 125.50, "paymentType": "Баланс"},
                {"advertId": 77, "updSum": 34.25, "paymentType": "Промо бонусы"},
            ]
        raise wb_module.IntegrationError("fullstats недоступен")

    monkeypatch.setattr(wb_module, "request_json", fake_request_json)
    integration = WildberriesIntegration({"api_token": "token"})
    payload = asyncio.run(integration.fetch(date(2026, 8, 25)))

    assert payload.total.ad_spend == Decimal("125.5")
    assert payload.total.ad_bonus_spend == Decimal("34.25")
    assert payload.authoritative_total_fields == {"ad_spend", "ad_bonus_spend"}
    assert payload.products == []
    assert payload.warnings == ["Товарная детализация рекламы WB не обновлена: fullstats недоступен"]


def test_wb_empty_cost_history_falls_back_to_fullstats_spend(monkeypatch):
    async def fake_request_json(client, method, url, **kwargs):
        if url.endswith("/supplier/orders") or url.endswith("/supplier/sales"):
            return []
        if url.endswith("/adv/v1/upd"):
            return []
        if url.endswith("/adv/v1/promotion/count"):
            return {"adverts": [{"status": 9, "advert_list": [{"advertId": 77}]}]}
        if url.endswith("/adv/v3/fullstats"):
            return [
                {
                    "advertId": 77,
                    "days": [
                        {
                            "date": "2026-08-25T00:00:00+03:00",
                            "apps": [
                                {
                                    "nms": [
                                        {"nmId": 1001, "name": "Товар 1", "sum": 99}
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ]
        raise AssertionError(url)

    monkeypatch.setattr(wb_module, "request_json", fake_request_json)
    payload = asyncio.run(
        WildberriesIntegration({"api_token": "token"}).fetch(date(2026, 8, 25))
    )

    assert payload.total.ad_spend == Decimal("99")
    assert payload.total.ad_bonus_spend == Decimal("0")
    assert payload.authoritative_total_fields == set()
    assert payload.warnings == [
        "История источников списания WB пока пуста. "
        "Расход из статистики рекламы временно сохранён как РК Денег."
    ]
