from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.database import build_engine, build_session_factory, init_database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        app_timezone="Europe/Moscow",
        wb_base_url="https://advert-api.wildberries.ru",
        wb_http_timeout_seconds=2,
        wb_request_interval_seconds=0,
        wb_info_request_interval_seconds=0,
    )


@pytest.fixture
def database(settings):
    engine = build_engine(settings)
    init_database(engine)
    session_factory = build_session_factory(engine)
    yield engine, session_factory
    engine.dispose()


@pytest.fixture
def fullstats_payload():
    return [
        {
            "advertId": 28000001,
            "days": [
                {
                    "date": "2026-07-27T00:00:00Z",
                    "apps": [
                        {
                            "appType": 32,
                            "nms": [
                                {
                                    "nmId": 699712395,
                                    "name": "Фотообои Горы",
                                    "views": 1000,
                                    "clicks": 40,
                                    "sum": 400,
                                    "atbs": 8,
                                    "orders": 2,
                                    "canceled": 1,
                                    "shks": 2,
                                    "sum_price": 4000,
                                }
                            ],
                        },
                        {
                            "appType": 64,
                            "nms": [
                                {
                                    "nmId": 699712395,
                                    "name": "Фотообои Горы",
                                    "views": 500,
                                    "clicks": 10,
                                    "sum": 100,
                                    "atbs": 2,
                                    "orders": 1,
                                    "canceled": 0,
                                    "shks": 1,
                                    "sum_price": 2000,
                                }
                            ],
                        },
                    ],
                }
            ],
        }
    ]


class FakeWBClient:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def ping(self):
        return True

    async def list_campaigns(self):
        return [
            {
                "advert_id": 28000001,
                "status": 9,
                "campaign_type": 8,
            },
            {
                "advert_id": 28000002,
                "status": 8,
                "campaign_type": 8,
            },
        ]

    async def campaign_details(self, campaign_ids):
        assert campaign_ids == [28000001]
        return [
            {
                "id": 28000001,
                "status": 9,
                "settings": {
                    "name": "Фотообои · поиск",
                    "payment_type": "cpm",
                },
                "nm_settings": [
                    {
                        "nm_id": 699712395,
                        "subject": {"id": 1, "name": "Фотообои"},
                    }
                ],
            }
        ]

    async def full_stats(self, campaign_ids, begin, end):
        assert campaign_ids == [28000001]
        assert begin <= end
        yield self.payload


@pytest.fixture
def fake_client_factory(fullstats_payload):
    return lambda _token: FakeWBClient(fullstats_payload)

