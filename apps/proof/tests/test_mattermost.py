from __future__ import annotations

import httpx
import app.main as main_module
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import session_factory, settings
from app.models import ProofEvent, ProofIntegration, ProofNotificationDelivery
from app.security import decrypt_secret, encrypt_secret, new_secret, secret_parts, token_digest
from app.services import (
    add_event,
    credential_cipher_for_settings,
    dispatch_pending_mattermost,
    json_dump,
)
from tests.helpers import bootstrap, webhook_payload, worker_headers


MATTERMOST_URL = "https://mattermost.example/hooks/private-webhook-value"


def add_mattermost(owner_id: int = 17, *, channel: str = "production-alerts") -> int:
    with session_factory() as session:
        secret = new_secret("proof_internal")
        prefix, last_four = secret_parts(secret)
        integration = ProofIntegration(
            owner_external_user_id=owner_id,
            kind="mattermost",
            enabled=True,
            trigger_events_json=json_dump({"channel": channel}),
            credentials_encrypted=encrypt_secret(
                credential_cipher_for_settings(settings), {"webhook_url": MATTERMOST_URL}
            ),
            webhook_secret_digest=token_digest(secret),
            webhook_secret_prefix=prefix,
            webhook_secret_last_four=last_four,
        )
        session.add(integration)
        session.commit()
        return integration.id


def test_mattermost_configuration_encrypts_webhook_and_never_renders_it(
    client: TestClient, identity_headers: dict[str, str]
) -> None:
    response = client.post(
        "/integrations/mattermost",
        headers=identity_headers,
        data={
            "csrf_token": "proof-csrf",
            "webhook_url": MATTERMOST_URL,
            "channel": "production-alerts",
            "enabled": "on",
        },
    )
    assert response.status_code == 200
    assert "Настройки Mattermost сохранены" in response.text
    assert "Отправить тестовое уведомление" in response.text
    assert MATTERMOST_URL not in response.text
    assert "private-webhook-value" not in response.text
    with session_factory() as session:
        integration = session.scalar(select(ProofIntegration).where(
            ProofIntegration.kind == "mattermost"
        ))
        assert integration.enabled is True
        assert MATTERMOST_URL not in integration.credentials_encrypted
        stored = decrypt_secret(
            credential_cipher_for_settings(settings), integration.credentials_encrypted
        )
        assert stored["webhook_url"] == MATTERMOST_URL


def test_standalone_mattermost_test_uses_saved_configuration(
    client: TestClient, identity_headers: dict[str, str], monkeypatch
) -> None:
    add_mattermost()
    delivered: dict[str, str] = {}

    def fake_post(_settings, webhook_url: str, channel: str, text: str) -> None:
        delivered.update(webhook_url=webhook_url, channel=channel, text=text)

    monkeypatch.setattr(main_module, "post_mattermost_message", fake_post)
    response = client.post(
        "/integrations/mattermost/test",
        headers=identity_headers,
        data={"csrf_token": "proof-csrf"},
    )

    assert response.status_code == 200
    assert "Тестовое уведомление Mattermost доставлено" in response.text
    assert delivered["webhook_url"] == MATTERMOST_URL
    assert delivered["channel"] == "production-alerts"
    assert delivered["text"] == "Тестовое уведомление доставлено. Интеграция Mattermost работает."
    assert MATTERMOST_URL not in response.text
    with session_factory() as session:
        integration = session.scalar(select(ProofIntegration).where(
            ProofIntegration.kind == "mattermost"
        ))
        assert integration.last_delivery_at is not None
        assert integration.last_error_message is None
        event = session.scalar(select(ProofEvent).where(
            ProofEvent.event_type == "mattermost.test.sent"
        ))
        assert event is not None


def test_mattermost_test_requires_saved_webhook(
    client: TestClient, identity_headers: dict[str, str]
) -> None:
    response = client.post(
        "/integrations/mattermost/test",
        headers=identity_headers,
        data={"csrf_token": "proof-csrf"},
    )

    assert response.status_code == 200
    assert "Сначала сохраните Incoming Webhook URL Mattermost" in response.text


def test_error_event_is_delivered_once_to_configured_mattermost(client: TestClient) -> None:
    setup = bootstrap()
    add_mattermost()
    webhook = client.post(f"/webhooks/amocrm/{setup['webhook_secret']}", json=webhook_payload())
    job_id = webhook.json()["job_id"]
    headers = worker_headers(str(setup["worker_token"]))
    client.post("/api/v1/jobs/claim", headers=headers)
    client.post(f"/api/v1/jobs/{job_id}/start", headers=headers)
    failed = client.post(
        f"/api/v1/jobs/{job_id}/fail",
        headers=headers,
        json={"error_code": "SOURCE_NOT_FOUND", "message": "Source file was not found.", "details": {}},
    )
    assert failed.status_code == 200

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        assert dispatch_pending_mattermost(session_factory, settings, client=http_client) == 1
        assert dispatch_pending_mattermost(session_factory, settings, client=http_client) == 0
    assert len(requests) == 1
    payload = requests[0].read().decode("utf-8")
    assert requests[0].url == MATTERMOST_URL
    assert "production-alerts" in payload
    assert "Исходный файл не найден." in payload
    assert "Номер заказа" in payload
    assert "33860843" in payload
    assert "Необходимо подготовить цветопробу вручную." in payload
    assert "SOURCE_NOT_FOUND" not in payload
    assert job_id not in payload
    with session_factory() as session:
        delivery = session.scalar(select(ProofNotificationDelivery))
        assert delivery.status == "sent"
        assert delivery.sent_at is not None


def test_info_event_does_not_create_mattermost_notification(client: TestClient) -> None:
    del client
    add_mattermost()
    with session_factory() as session:
        add_event(
            session,
            owner_external_user_id=17,
            event_type="test.info",
            source="core",
            level="info",
            message="Routine event.",
        )
        session.commit()
        assert session.scalar(select(ProofNotificationDelivery)) is None


def test_repeated_worker_heartbeat_error_does_not_spam_notifications(client: TestClient) -> None:
    setup = bootstrap()
    add_mattermost()
    payload = {
        "hostname": "proof-node-01",
        "version": "1.0.0",
        "availability": "error",
        "capabilities": ["proof-v1"],
        "current_job_id": None,
        "last_error_code": "DISK_UNAVAILABLE",
        "last_error_message": "Output disk is unavailable.",
    }
    headers = worker_headers(str(setup["worker_token"]))
    assert client.post("/api/v1/workers/heartbeat", headers=headers, json=payload).status_code == 200
    assert client.post("/api/v1/workers/heartbeat", headers=headers, json=payload).status_code == 200
    with session_factory() as session:
        notifications = list(session.scalars(select(ProofNotificationDelivery)))
        assert len(notifications) == 1


def test_failed_mattermost_delivery_is_logged_without_recursive_notification_or_url(
    client: TestClient,
) -> None:
    del client
    integration_id = add_mattermost()
    with session_factory() as session:
        add_event(
            session,
            owner_external_user_id=17,
            event_type="test.error",
            source="core",
            level="error",
            message="A controlled test error occurred.",
        )
        session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        assert dispatch_pending_mattermost(session_factory, settings, client=http_client) == 0
    with session_factory() as session:
        deliveries = list(session.scalars(select(ProofNotificationDelivery)))
        integration = session.get(ProofIntegration, integration_id)
        events = list(session.scalars(select(ProofEvent)))
        serialized = " ".join(
            (event.message or "") + (event.details_json or "") for event in events
        ) + " " + (integration.last_error_message or "")
        assert len(deliveries) == 1
        assert deliveries[0].status == "failed"
        assert "private-webhook-value" not in serialized
        assert sum(event.event_type == "mattermost.notification.failed" for event in events) == 1
