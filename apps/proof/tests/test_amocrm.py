from __future__ import annotations

import io
import zipfile
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main as main_module
import app.services as services_module
from app.amocrm import AmoFieldMapping, AmoIntegrationConfiguration
from app.main import session_factory
from app.models import ProofIntegration, ProofJob, ProofResultDelivery
from app.services import json_dump, json_load
from tests.helpers import bootstrap


class FakeAmoClient:
    lead: dict[str, Any] = {}
    updates: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def get_lead(self, _lead_id: str) -> dict[str, Any]:
        return self.lead

    def update_lead(self, lead_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.updates.append((lead_id, payload))
        return {"id": int(lead_id)}


class FakeDirectAmoClient:
    upload_calls = 0
    note_calls = 0
    fail_note_once = True
    remote_note_exists = False
    archive_bytes = b""
    updates: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def upload_file(self, file_obj, **_kwargs: Any) -> dict[str, Any]:
        type(self).upload_calls += 1
        type(self).archive_bytes = file_obj.read()
        return {"uuid": "file-uuid", "version_uuid": "version-uuid"}

    def find_attachment_note(self, _lead_id: str, _file_uuid: str) -> dict[str, Any] | None:
        return {"id": 4321} if type(self).remote_note_exists else None

    def create_attachment_note(self, _lead_id: str, **_kwargs: Any) -> dict[str, Any]:
        type(self).note_calls += 1
        if type(self).fail_note_once:
            type(self).fail_note_once = False
            type(self).remote_note_exists = True
            raise RuntimeError("temporary note failure")
        return {"id": 4321}

    def update_lead(self, lead_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        type(self).updates.append((lead_id, payload))
        return {"id": int(lead_id)}


def configured_amocrm() -> AmoIntegrationConfiguration:
    return AmoIntegrationConfiguration(
        api_base_url="https://company.amocrm.ru",
        api_timeout_seconds=8,
        incoming_pipeline_id=77,
        incoming_status_id=88,
        queued_status_id=89,
        mappings=[
            AmoFieldMapping(target="source_path", field_id=1001),
            AmoFieldMapping(target="layout_number", field_id=1002),
            AmoFieldMapping(target="order_number", field_id=1003),
        ],
        statuses_cache=[{
            "id": 89,
            "name": "В очереди Proof",
            "pipeline_id": 77,
            "pipeline_name": "Production",
        }],
    )


def test_amocrm_account_url_cannot_send_token_to_an_arbitrary_host() -> None:
    with pytest.raises(ValidationError):
        AmoIntegrationConfiguration(
            api_base_url="https://attacker.example",
            api_timeout_seconds=8,
        )


def official_webhook() -> dict[str, str]:
    return {
        "account[id]": "1234",
        "leads[status][0][id]": "7654321",
        "leads[status][0][last_modified]": "1788700000",
        "leads[status][0][status_id]": "88",
    }


def test_ui_saves_exactly_three_selected_amocrm_fields(
    client: TestClient,
    identity_headers: dict[str, str],
) -> None:
    setup = bootstrap()
    response = client.post(
        "/integrations/amocrm",
        headers=identity_headers,
        data={
            "csrf_token": "proof-csrf",
            "trigger_events": "leads.status",
            "default_preset_id": str(setup["preset_id"]),
            "api_base_url": "https://company.amocrm.ru",
            "api_timeout_seconds": "8",
            "queued_status_id": "89",
            "completed_status_id": "90",
            "source_path_field_id": "1001",
            "layout_number_field_id": "1002",
            "third_field_id": "1003",
            "third_target": "order_number",
            "clear_field_one_id": "1001",
            "clear_field_two_id": "1002",
            "delivery_mode": "amocrm_attachment",
            "enabled": "on",
        },
    )
    assert response.status_code == 200
    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        configuration = AmoIntegrationConfiguration.model_validate(
            json_load(integration.configuration_json, {})
        )
        assert [(item.target, item.field_id) for item in configuration.mappings] == [
            ("source_path", 1001),
            ("layout_number", 1002),
            ("order_number", 1003),
        ]
        assert configuration.clear_field_ids == [1001, 1002]
        assert configuration.delivery_mode == "amocrm_attachment"


def test_official_webhook_reads_only_selected_fields_and_moves_lead_to_queue(
    client: TestClient,
    monkeypatch,
) -> None:
    setup = bootstrap()
    configuration = configured_amocrm()
    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        integration.configuration_json = json_dump(configuration.model_dump(mode="json"))
        session.commit()
    FakeAmoClient.lead = {
        "id": 7654321,
        "pipeline_id": 77,
        "status_id": 88,
        "custom_fields_values": [
            {"field_id": 1001, "values": [{"value": r"\\ip\дизайн отдел\Макеты (опт)\Сентябрь 2026\33860843"}]},
            {"field_id": 1002, "values": [{"value": "4"}]},
            {"field_id": 1003, "values": [{"value": "33860843"}]},
            {"field_id": 9999, "values": [{"value": "must-not-enter-job"}]},
        ],
    }
    FakeAmoClient.updates = []
    monkeypatch.setattr(main_module, "AmoClient", FakeAmoClient)

    response = client.post(
        f"/webhooks/amocrm/{setup['webhook_secret']}",
        data=official_webhook(),
    )
    assert response.status_code == 202
    with session_factory() as session:
        job = session.get(ProofJob, response.json()["job_id"])
        payload = json_load(job.input_json, {})
        assert payload["source_path"].endswith(r"Сентябрь 2026\33860843")
        assert payload["layout_number"] == 4
        assert payload["order_number"] == "33860843"
        assert "must-not-enter-job" not in job.input_json
    assert FakeAmoClient.updates == [("7654321", {"status_id": 89, "pipeline_id": 77})]


def test_official_webhook_is_ignored_when_current_status_no_longer_matches(
    client: TestClient,
    monkeypatch,
) -> None:
    setup = bootstrap()
    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        integration.configuration_json = json_dump(configured_amocrm().model_dump(mode="json"))
        session.commit()
    FakeAmoClient.lead = {
        "id": 7654321,
        "pipeline_id": 77,
        "status_id": 999,
        "custom_fields_values": [],
    }
    FakeAmoClient.updates = []
    monkeypatch.setattr(main_module, "AmoClient", FakeAmoClient)

    response = client.post(
        f"/webhooks/amocrm/{setup['webhook_secret']}",
        data=official_webhook(),
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": False, "duplicate": False, "job_id": None}
    with session_factory() as session:
        assert session.query(ProofJob).count() == 0
    assert FakeAmoClient.updates == []


def test_official_webhook_rejects_incomplete_field_mapping(
    client: TestClient,
    monkeypatch,
) -> None:
    setup = bootstrap()
    incomplete = configured_amocrm().model_copy(
        update={
            "mappings": [
                AmoFieldMapping(target="source_path", field_id=1001),
                AmoFieldMapping(target="layout_number", field_id=1002),
            ]
        }
    )
    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        integration.configuration_json = json_dump(incomplete.model_dump(mode="json"))
        session.commit()
    FakeAmoClient.updates = []
    monkeypatch.setattr(main_module, "AmoClient", FakeAmoClient)

    response = client.post(
        f"/webhooks/amocrm/{setup['webhook_secret']}",
        data=official_webhook(),
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Не удалось получить данные сделки amoCRM."}
    with session_factory() as session:
        assert session.query(ProofJob).count() == 0
    assert FakeAmoClient.updates == []


def test_amocrm_file_upload_uses_drive_session_and_chunks() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v4/account":
            return httpx.Response(200, json={"drive_url": "https://drive-b.amocrm.ru"})
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json={
                "upload_url": "https://drive-b.amocrm.ru/upload/part-1",
                "max_part_size": 4,
            })
        if request.url.path == "/upload/part-1":
            return httpx.Response(200, json={
                "next_url": "https://drive-b.amocrm.ru/upload/part-2",
            })
        if request.url.path == "/upload/part-2":
            return httpx.Response(200, json={
                "uuid": "file-uuid",
                "version_uuid": "version-uuid",
            })
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with main_module.AmoClient(
            "https://company.amocrm.ru",
            "secret-token",
            timeout_seconds=8,
            client=http_client,
        ) as amo:
            uploaded = amo.upload_file(
                io.BytesIO(b"abcdef"),
                file_name="proof.zip",
                file_size=6,
                content_type="application/zip",
            )

    assert uploaded == {"uuid": "file-uuid", "version_uuid": "version-uuid"}
    assert [request.content for request in requests[-2:]] == [b"abcd", b"ef"]
    assert all(request.headers["authorization"] == "Bearer secret-token" for request in requests)


def test_amocrm_catalog_loads_all_custom_field_pages() -> None:
    requested_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_pages.append(str(request.url))
        page = request.url.params.get("page")
        if request.url.path.endswith("/custom_fields"):
            return httpx.Response(200, json={
                "_embedded": {"custom_fields": [{
                    "id": 1000 + int(page),
                    "name": f"Field {page}",
                    "type": "text",
                }]},
                "_links": {"next": {"href": "next"}} if page == "1" else {},
            })
        if request.url.path.endswith("/pipelines"):
            return httpx.Response(200, json={"_embedded": {"pipelines": []}, "_links": {}})
        raise AssertionError(f"Unexpected request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with main_module.AmoClient(
            "https://company.amocrm.ru",
            "secret-token",
            timeout_seconds=8,
            client=http_client,
        ) as amo:
            fields, statuses = amo.load_catalog()

    assert [field["id"] for field in fields] == [1001, 1002]
    assert statuses == []
    assert any("custom_fields?limit=250&page=2" in url for url in requested_pages)


def test_duplicate_worker_complete_recovers_direct_delivery_without_duplicate_attachment(
    client: TestClient,
    monkeypatch,
) -> None:
    setup = bootstrap()
    configuration = configured_amocrm().model_copy(update={
        "completed_status_id": 90,
        "delivery_mode": "amocrm_attachment",
        "clear_field_ids": [1001, 1002],
        "statuses_cache": [{
            "id": 90,
            "name": "Proof completed",
            "pipeline_id": 77,
            "pipeline_name": "Production",
        }],
    })
    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        integration.configuration_json = json_dump(configuration.model_dump(mode="json"))
        integration.delivery_url = ""
        session.commit()

    FakeDirectAmoClient.upload_calls = 0
    FakeDirectAmoClient.note_calls = 0
    FakeDirectAmoClient.fail_note_once = True
    FakeDirectAmoClient.remote_note_exists = False
    FakeDirectAmoClient.archive_bytes = b""
    FakeDirectAmoClient.updates = []
    monkeypatch.setattr(services_module, "AmoClient", FakeDirectAmoClient)

    job_response = client.post(
        f"/webhooks/amocrm/{setup['webhook_secret']}",
        json={
            "event_id": "direct-delivery",
            "event_type": "proof.requested",
            "crm_entity_type": "leads",
            "crm_entity_id": "7654321",
            "crm_order_id": "ORDER-42",
            "input": {"source_path": "orders/ORDER-42", "layout_number": 3},
        },
    )
    job_id = job_response.json()["job_id"]
    headers = {"Authorization": f"Bearer {setup['worker_token']}"}
    client.post("/api/v1/jobs/claim", headers=headers)
    client.post(f"/api/v1/jobs/{job_id}/start", headers=headers)
    client.post(
        f"/api/v1/jobs/{job_id}/result",
        headers={**headers, "Idempotency-Key": "direct-result"},
        files={"file": ("proof.jpg", b"proof-image", "image/jpeg")},
    )

    first = client.post(f"/api/v1/jobs/{job_id}/complete", headers=headers)
    assert first.status_code == 200
    assert first.json()["delivery_status"] == "failed"
    with session_factory() as session:
        delivery = session.query(ProofResultDelivery).one()
        assert delivery.amo_file_uuid == "file-uuid"
        assert delivery.amo_note_id is None

    retry = client.post(f"/api/v1/jobs/{job_id}/complete", headers=headers)
    assert retry.status_code == 200
    assert retry.json()["delivery_status"] == "delivered"

    assert FakeDirectAmoClient.upload_calls == 1
    assert FakeDirectAmoClient.note_calls == 1
    with zipfile.ZipFile(io.BytesIO(FakeDirectAmoClient.archive_bytes)) as archive:
        assert archive.namelist() == ["proof.jpg"]
        assert archive.read("proof.jpg") == b"proof-image"
    assert FakeDirectAmoClient.updates == [(
        "7654321",
        {
            "status_id": 90,
            "pipeline_id": 77,
            "custom_fields_values": [
                {"field_id": 1001, "values": None},
                {"field_id": 1002, "values": None},
            ],
        },
    )]
    with session_factory() as session:
        job = session.get(ProofJob, job_id)
        delivery = session.query(ProofResultDelivery).one()
        assert job.processing_status == "completed"
        assert job.delivery_status == "delivered"
        assert delivery.amo_note_id == 4321
        assert delivery.finalized_at is not None
