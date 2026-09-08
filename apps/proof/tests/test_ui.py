from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import bootstrap, webhook_payload


def test_ui_requires_platform_identity(client: TestClient) -> None:
    for path in ("/", "/jobs", "/queue", "/workers", "/results", "/presets", "/integrations", "/logs", "/settings"):
        assert client.get(path).status_code == 401


def test_all_sections_render_inside_bellenne_shell(
    client: TestClient, identity_headers: dict[str, str]
) -> None:
    bootstrap()
    for path, marker in (
        ("/", "BellenneProof"), ("/jobs", "Задания"), ("/queue", "Очередь"),
        ("/workers", "Workers"), ("/results", "Результаты"), ("/presets", "Presets"),
        ("/integrations", "amoCRM"), ("/logs", "Proof Log"), ("/settings", "Worker API"),
    ):
        response = client.get(path, headers=identity_headers)
        assert response.status_code == 200, response.text
        assert marker in response.text
        assert "/pulse/" in response.text and "/nest/" in response.text


def test_job_details_show_processing_delivery_worker_and_timeline(
    client: TestClient, identity_headers: dict[str, str]
) -> None:
    setup = bootstrap()
    webhook = client.post(f"/webhooks/amocrm/{setup['webhook_secret']}", json=webhook_payload())
    job_id = webhook.json()["job_id"]
    detail = client.get(f"/jobs/{job_id}", headers=identity_headers)
    assert detail.status_code == 200
    assert "Processing" in detail.text
    assert "Delivery" in detail.text
    assert "Timeline" in detail.text
    assert "В очереди" in detail.text
    assert "Production v1" in detail.text


def test_ui_mutations_require_platform_csrf(
    client: TestClient, identity_headers: dict[str, str]
) -> None:
    bootstrap()
    response = client.post(
        "/presets", headers=identity_headers,
        data={"csrf_token": "wrong", "name": "Unsafe", "parameters_json": "{}"},
    )
    assert response.status_code == 403
