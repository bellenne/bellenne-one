from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import session_factory
from app.models import ApiToken
from tests.conftest import TEST_TOKEN


def settings_form(csrf_token: str, **overrides) -> dict[str, str]:
    values = {
        "csrf_token": csrf_token,
        "roll_length_m": "50",
        "target_min_m": "47",
        "upper_tolerance_m": "1.5",
        "lower_soft_margin_m": "0",
        "job_gap_cm": "15",
        "leader_cm": "50",
        "trailer_cm": "50",
        "panel_gap_cm": "0",
        "top_bottom_margin_cm": "0",
        "default_height_cm": "270",
        "brute_force_limit": "26",
        "partial_max_r": "10",
        "length_mode": "per_panel",
        "completion_objective": "max_groups",
        "completion_exact_groups": "0",
        "completion_allowed_add_widths": "300, 200, 100, 500",
        "use_qty": "on",
    }
    values.update(overrides)
    return values


def test_ui_requires_platform_identity(client: TestClient) -> None:
    assert client.get("/").status_code == 401
    assert client.get("/configuration").status_code == 401
    assert client.get("/history").status_code == 401


def test_ui_pages_use_bellenne_shell(
    client: TestClient,
    identity_headers: dict[str, str],
) -> None:
    for path, marker in (
        ("/", "BellenneNest"),
        ("/configuration", "Конфигурация"),
        ("/api-info", "Nest API"),
        ("/history", "История запросов"),
    ):
        response = client.get(path, headers=identity_headers)
        assert response.status_code == 200
        assert marker in response.text
        assert "/pulse/" in response.text
        assert "/echo/" in response.text
        assert "/vector/" in response.text


def test_saved_user_configuration_is_used_when_api_omits_settings(
    client: TestClient,
    identity_headers: dict[str, str],
) -> None:
    save_response = client.post(
        "/configuration",
        headers=identity_headers,
        data=settings_form("test-csrf-token", target_min_m="40", roll_length_m="42"),
        follow_redirects=False,
    )
    assert save_response.status_code == 303
    assert save_response.headers["location"] == "/nest/configuration?saved=1"

    response = client.post(
        "/api/v1/calculate",
        headers={"AUTH-TOKEN": TEST_TOKEN, "X-MES-User": "mes.settings-user"},
        json={
            "items": [
                {
                    "article": "ONE_100x270",
                    "width_cm": 100,
                    "height_cm": 270,
                    "quantity": 17,
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["settings_used"]["target_min_m"] == 40.0

    explicit_response = client.post(
        "/api/v1/calculate",
        headers={"AUTH-TOKEN": TEST_TOKEN, "X-MES-User": "mes.settings-user"},
        json={
            "items": [{"article": "ONE_100x270", "quantity": 17}],
            "settings": {"target_min_m": 47, "roll_length_m": 50},
        },
    )
    assert explicit_response.status_code == 200
    assert explicit_response.json()["settings_used"]["target_min_m"] == 47.0


def test_configuration_rejects_invalid_range_and_csrf(
    client: TestClient,
    identity_headers: dict[str, str],
) -> None:
    invalid = client.post(
        "/configuration",
        headers=identity_headers,
        data=settings_form("test-csrf-token", target_min_m="51", roll_length_m="50"),
    )
    assert invalid.status_code == 200
    assert "Конфигурация не сохранена" in invalid.text

    forbidden = client.post(
        "/configuration",
        headers=identity_headers,
        data=settings_form("wrong-token"),
    )
    assert forbidden.status_code == 403


def test_history_has_human_readable_result(
    client: TestClient,
    identity_headers: dict[str, str],
) -> None:
    calculation = client.post(
        "/api/v1/calculate",
        headers={"AUTH-TOKEN": TEST_TOKEN, "X-MES-User": "mes.history-user"},
        json={"items": [{"article": "ONE_100x270", "quantity": 17}]},
    )
    request_id = calculation.headers["X-Nest-Request-Id"]
    history = client.get("/history", headers=identity_headers)
    detail = client.get(f"/history/{request_id}", headers=identity_headers)
    assert request_id[:8] in history.text
    assert "Сводка результата" in detail.text
    assert "Эффективные раскладки" in detail.text
    assert "ONE_100x270" in detail.text
    assert "пользователь MES: mes.history-user" in detail.text


def test_history_shows_rolls_and_material_remainder(
    client: TestClient,
    identity_headers: dict[str, str],
) -> None:
    calculation = client.post(
        "/api/v1/calculate",
        headers={"AUTH-TOKEN": TEST_TOKEN, "X-MES-User": "mes.roll-user"},
        json={
            "items": [{"article": "ONE_100x270", "quantity": 18}],
            "settings": {"roll_length_m": 50, "target_min_m": 47},
        },
    )
    request_id = calculation.headers["X-Nest-Request-Id"]
    history = client.get("/history", headers=identity_headers)
    detail = client.get(f"/history/{request_id}", headers=identity_headers)

    assert "mes.roll-user" in history.text
    assert "51.3 м" in history.text
    assert "48.7 м" in history.text
    assert "Остаток: 1 ед." in history.text
    assert "Затрачено рулонов" in detail.text
    assert "Эффективных рулонов" in detail.text
    assert "Остаточных рулонов" in detail.text
    assert "Остаток материала" in detail.text
    assert "48.7 м" in detail.text


def test_api_token_is_generated_per_account_and_shown_once(
    client: TestClient,
    identity_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api-token/generate",
        headers=identity_headers,
        data={"csrf_token": "test-csrf-token"},
    )
    assert response.status_code == 200
    match = re.search(r"bn_nest_[A-Za-z0-9_-]+", response.text)
    assert match is not None
    generated_token = match.group(0)
    assert generated_token != TEST_TOKEN
    assert client.post(
        "/api/v1/calculate",
        headers={"AUTH-TOKEN": TEST_TOKEN, "X-MES-User": "mes.old-token"},
        json={"items": [{"article": "ONE_100x270", "quantity": 17}]},
    ).status_code == 401

    page = client.get("/api-info", headers=identity_headers)
    assert page.status_code == 200
    assert generated_token not in page.text
    assert "Показывается один раз" not in page.text

    with session_factory() as session:
        credential = session.scalar(
            select(ApiToken).where(ApiToken.external_user_id == 17)
        )
        assert credential is not None
        assert credential.token_digest not in {"", generated_token}
        assert credential.owner_username == "nest-owner"


def test_api_token_generation_requires_platform_csrf(
    client: TestClient,
    identity_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api-token/generate",
        headers=identity_headers,
        data={"csrf_token": "wrong-token"},
    )
    assert response.status_code == 403


def test_different_accounts_receive_different_tokens(
    client: TestClient,
    identity_headers: dict[str, str],
) -> None:
    first = client.post(
        "/api-token/generate",
        headers=identity_headers,
        data={"csrf_token": "test-csrf-token"},
    )
    first_token = re.search(r"bn_nest_[A-Za-z0-9_-]+", first.text).group(0)
    other_identity = {
        "X-Bellenne-User-Id": "18",
        "X-Bellenne-Username": "other-owner",
        "X-Bellenne-Csrf-Token": "other-csrf",
    }
    second = client.post(
        "/api-token/generate",
        headers=other_identity,
        data={"csrf_token": "other-csrf"},
    )
    second_token = re.search(r"bn_nest_[A-Za-z0-9_-]+", second.text).group(0)
    assert first_token != second_token

    with session_factory() as session:
        credentials = list(session.scalars(select(ApiToken)))
        assert {item.external_user_id for item in credentials} >= {17, 18}


def test_generated_token_owns_configuration_and_history(
    client: TestClient,
    identity_headers: dict[str, str],
) -> None:
    generated = client.post(
        "/api-token/generate",
        headers=identity_headers,
        data={"csrf_token": "test-csrf-token"},
    )
    token = re.search(r"bn_nest_[A-Za-z0-9_-]+", generated.text).group(0)
    calculation = client.post(
        "/api/v1/calculate",
        headers={"AUTH-TOKEN": token, "X-MES-User": "mes.anna"},
        json={"items": [{"article": "ONE_100x270", "quantity": 17}]},
    )
    assert calculation.status_code == 200
    history = client.get("/history", headers=identity_headers)
    assert "mes.anna" in history.text

    other_identity = {
        "X-Bellenne-User-Id": "18",
        "X-Bellenne-Username": "other-owner",
        "X-Bellenne-Csrf-Token": "other-csrf",
    }
    other_history = client.get("/history", headers=other_identity)
    assert "mes.anna" not in other_history.text
