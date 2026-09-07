from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import Settings, process_images
from app.main import session_factory
from app.models import RequestLog
from app.services import summarize_calculation_result
from tests.conftest import TEST_TOKEN


def calculation_payload() -> dict:
    return {
        "items": [
            {
                "article": "ONE_100x270",
                "width_cm": 100,
                "height_cm": 270,
                "quantity": 17,
                "laravel_id": 123,
            }
        ]
    }


def test_health_contract_is_preserved(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert client.get("/healthz").json()["service"] == "BellenneNest"


def test_calculate_requires_original_auth_header(client: TestClient) -> None:
    assert client.post("/api/v1/calculate", json=calculation_payload()).status_code == 401


def test_calculate_requires_mes_user(client: TestClient) -> None:
    response = client.post(
        "/api/v1/calculate",
        headers={"AUTH-TOKEN": TEST_TOKEN},
        json=calculation_payload(),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "X-MES-User header is required."


def test_calculate_preserves_result_and_records_user(
    client: TestClient,
    identity_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/v1/calculate",
        headers={
            "AUTH-TOKEN": TEST_TOKEN,
            "X-MES-User": "mes.operator",
            "X-Bellenne-User-Id": "999",
            "X-Bellenne-Username": "spoofed-owner",
        },
        json=calculation_payload(),
    )
    assert response.status_code == 200
    assert response.headers["X-Nest-Request-Id"]
    body = response.json()
    assert body["is_ideal"] is True
    assert body["summary"]["input_units"] == 17
    assert body["impositions"][0]["item_counts"] == {"ONE_100x270": 17}

    with session_factory() as session:
        log = session.scalar(
            select(RequestLog).where(RequestLog.id == response.headers["X-Nest-Request-Id"])
        )
        assert log is not None
        assert log.external_user_id == 17
        assert log.username == "mes.operator"
        assert log.status_code == 200
        assert log.item_count == 1
        assert log.is_ideal is True


def test_invalid_request_is_recorded_in_history(
    client: TestClient,
    identity_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/v1/calculate",
        headers={"AUTH-TOKEN": TEST_TOKEN, "X-MES-User": "mes.validator"},
        json={"items": [{"id": 1}]},
    )
    assert response.status_code == 422
    with session_factory() as session:
        log = session.scalar(
            select(RequestLog)
            .where(RequestLog.status_code == 422)
            .order_by(RequestLog.created_at.desc())
        )
        assert log is not None
        assert log.username == "mes.validator"
        assert "проверку входных данных" in (log.error_message or "")


def test_original_defaults_endpoint_is_preserved(client: TestClient) -> None:
    response = client.get(
        "/settings/defaults",
        headers={"AUTH-TOKEN": TEST_TOKEN},
    )
    assert response.status_code == 200
    assert response.json()["target_min_m"] == 47.0
    assert response.json()["default_height_cm"] == 270


def test_roll_usage_summary_uses_real_imposition_repeats() -> None:
    result = process_images(
        [{"article": "ONE_100x270", "width_cm": 100, "height_cm": 270, "qty": 18}],
        Settings(),
    )
    usage = summarize_calculation_result(result)
    assert usage == {
        "rolls_used": 2,
        "efficient_rolls": 1,
        "remainder_rolls": 1,
        "material_used_m": 51.3,
        "material_remaining_m": 48.7,
        "remaining_units": 1,
        "unplaced_units": 0,
    }
