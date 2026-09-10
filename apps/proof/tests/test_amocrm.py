from __future__ import annotations

import hashlib
import hmac
import io
import json
import time
import zipfile
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main as main_module
import app.services as services_module
from app.amocrm import AmoFieldMapping, AmoIntegrationConfiguration, AmoOAuthTokenSet
from app.main import session_factory, settings
from app.models import ProofEvent, ProofIntegration, ProofJob, ProofResultDelivery
from app.security import encrypt_secret, token_digest
from app.services import credential_cipher_for_settings, json_dump, json_load
from tests.helpers import bootstrap


class FakeAmoClient:
    lead: dict[str, Any] = {}
    updates: list[tuple[str, dict[str, Any]]] = []
    get_calls = 0
    fail_update = False

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def get_lead(self, _lead_id: str) -> dict[str, Any]:
        type(self).get_calls += 1
        return self.lead

    def update_lead(self, lead_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if type(self).fail_update:
            raise RuntimeError("temporary update failure")
        self.updates.append((lead_id, payload))
        return {"id": int(lead_id)}


class FakeOAuthAccountClient:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def get_account(self) -> dict[str, Any]:
        return {"id": 1234, "name": "CustomCraft"}


class FakeDirectAmoClient:
    upload_calls = 0
    link_calls = 0
    fail_link_once = True
    remote_link_exists = False
    uploaded_bytes = b""
    uploaded_options: dict[str, Any] = {}
    updates: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def upload_file(self, file_obj, **kwargs: Any) -> dict[str, Any]:
        type(self).upload_calls += 1
        type(self).uploaded_bytes = file_obj.read()
        type(self).uploaded_options = kwargs
        return {"uuid": "file-uuid", "version_uuid": "version-uuid"}

    def is_file_linked(self, _lead_id: str, _file_uuid: str) -> bool:
        return type(self).remote_link_exists

    def link_file(self, _lead_id: str, _file_uuid: str) -> None:
        type(self).link_calls += 1
        type(self).remote_link_exists = True
        if type(self).fail_link_once:
            type(self).fail_link_once = False
            raise RuntimeError("temporary link failure")

    def update_lead(self, lead_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        type(self).updates.append((lead_id, payload))
        return {"id": int(lead_id)}


class FakeYandexDiskClient:
    upload_calls = 0
    uploaded_bytes = b""
    uploaded_path = ""
    folder_path = ""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def account(self) -> dict[str, Any]:
        return {"user": {"login": "production"}}

    def ensure_folder(self, path: str) -> None:
        type(self).folder_path = path

    def upload(self, path: str, file_obj) -> None:
        type(self).upload_calls += 1
        type(self).uploaded_path = path
        type(self).uploaded_bytes = file_obj.read()

    def publish(self, _path: str) -> str:
        return "https://disk.yandex.ru/d/proof-archive"


class FakeYandexAmoClient:
    create_note_calls = 0
    fail_note_once = True
    remote_note_exists = False
    updates: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def find_common_note(self, _lead_id: str, _text: str) -> dict[str, Any] | None:
        return {"id": 701} if type(self).remote_note_exists else None

    def create_common_note(self, _lead_id: str, _text: str) -> dict[str, Any]:
        type(self).create_note_calls += 1
        type(self).remote_note_exists = True
        if type(self).fail_note_once:
            type(self).fail_note_once = False
            raise RuntimeError("temporary note failure")
        return {"id": 701}

    def update_lead(self, lead_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        type(self).updates.append((lead_id, payload))
        return {"id": int(lead_id)}


def configured_amocrm() -> AmoIntegrationConfiguration:
    return AmoIntegrationConfiguration(
        api_base_url="https://company.amocrm.ru",
        api_timeout_seconds=8,
        queued_status_id=89,
        completed_status_id=90,
        mappings=[
            AmoFieldMapping(target="source_path", field_id=1001),
            AmoFieldMapping(target="layout_number", field_id=1002),
            AmoFieldMapping(target="public_id", field_id=1003),
        ],
        clear_field_ids=[1001, 1002],
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
        "leads[status][0][name]": "33860843",
        "leads[status][0][last_modified]": "1788700000",
        "leads[status][0][status_id]": "88",
    }


def test_ui_saves_designer_alongside_three_required_amocrm_fields(
    client: TestClient,
    identity_headers: dict[str, str],
) -> None:
    setup = bootstrap()
    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        configuration = AmoIntegrationConfiguration(fields_cache=[
            {"id": 1001, "name": "Путь к папке заказа", "type": "text"},
            {"id": 1002, "name": "Номер макета", "type": "numeric"},
            {"id": 1003, "name": "Запустить Proof", "type": "checkbox"},
            {"id": 1004, "name": "Дизайнер", "type": "text"},
        ])
        integration.configuration_json = json_dump(configuration.model_dump(mode="json"))
        session.commit()
    response = client.post(
        "/integrations/amocrm/webhook",
        headers=identity_headers,
        data={
            "csrf_token": "proof-csrf",
            "source_path_field_id": "1001",
            "layout_number_field_id": "1002",
            "third_field_id": "1003",
            "designer_field_id": "1004",
            "clear_source_path": "on",
            "clear_layout_number": "on",
        },
    )
    assert response.status_code == 200
    assert '<select class="select" id="source-path-field"' in response.text
    assert "Путь к папке заказа · ID 1001 · text" in response.text
    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        configuration = AmoIntegrationConfiguration.model_validate(
            json_load(integration.configuration_json, {})
        )
        assert [(item.target, item.field_id) for item in configuration.mappings] == [
            ("source_path", 1001),
            ("layout_number", 1002),
            ("public_id", 1003),
            ("designer_name", 1004),
        ]
        assert configuration.clear_field_ids == [1001, 1002, 1003]


def test_ui_checks_and_saves_yandex_disk_token_encrypted(
    client: TestClient,
    identity_headers: dict[str, str],
    monkeypatch,
) -> None:
    setup = bootstrap()
    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        integration.configuration_json = json_dump(
            configured_amocrm().model_dump(mode="json")
        )
        session.commit()
    monkeypatch.setattr(main_module, "YandexDiskClient", FakeYandexDiskClient)

    response = client.post(
        "/integrations/amocrm/yandex-disk",
        headers=identity_headers,
        data={
            "csrf_token": "proof-csrf",
            "oauth_token": "yandex-secret-token",
        },
    )

    assert response.status_code == 200
    assert "Яндекс.Диск подключён." in response.text
    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        configuration = AmoIntegrationConfiguration.model_validate(
            json_load(integration.configuration_json, {})
        )
        credentials = services_module.amocrm_credentials(integration, settings)
        assert configuration.yandex_disk_root == "amoCRM/Сделки"
        assert configuration.delivery_mode == "yandex_disk_note"
        assert credentials["yandex_disk_token"] == "yandex-secret-token"
        assert "yandex-secret-token" not in integration.credentials_encrypted


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
            {"field_id": 1003, "values": [{"value": True}]},
            {"field_id": 9999, "values": [{"value": "must-not-enter-job"}]},
        ],
    }
    FakeAmoClient.updates = []
    FakeAmoClient.get_calls = 0
    FakeAmoClient.fail_update = False
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
        assert payload["public_id"] == "True"
        assert job.crm_order_id == "7654321"
        assert "must-not-enter-job" not in job.input_json
        assert job.processing_status == "queued"
    assert FakeAmoClient.updates == [(
        "7654321",
        {
            "status_id": 89,
            "pipeline_id": 77,
            "custom_fields_values": [
                {"field_id": 1001, "values": None},
                {"field_id": 1002, "values": None},
                {"field_id": 1003, "values": None},
            ],
        },
    )]


def test_each_official_webhook_creates_an_independent_job_for_the_same_lead(
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
        "custom_fields_values": [
            {"field_id": 1001, "values": [{"value": r"\\ip\orders\33860843"}]},
            {"field_id": 1002, "values": [{"value": "4"}]},
            {"field_id": 1003, "values": [{"value": "33860843"}]},
        ],
    }
    FakeAmoClient.updates = []
    FakeAmoClient.get_calls = 0
    FakeAmoClient.fail_update = True
    monkeypatch.setattr(main_module, "AmoClient", FakeAmoClient)

    first = client.post(
        f"/webhooks/amocrm/{setup['webhook_secret']}",
        data=official_webhook(),
    )
    assert first.status_code == 502
    with session_factory() as session:
        job = session.query(ProofJob).one()
        assert job.processing_status == "received"

    FakeAmoClient.fail_update = False
    retry = client.post(
        f"/webhooks/amocrm/{setup['webhook_secret']}",
        data=official_webhook(),
    )
    assert retry.status_code == 202
    assert retry.json()["duplicate"] is False
    with session_factory() as session:
        jobs = session.query(ProofJob).order_by(ProofJob.created_at).all()
        assert len(jobs) == 2
        assert jobs[0].processing_status == "received"
        assert jobs[1].processing_status == "queued"
    assert FakeAmoClient.get_calls == 2
    assert len(FakeAmoClient.updates) == 1


def test_official_webhook_does_not_repeat_amo_conditions_inside_core(
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
        "custom_fields_values": [
            {"field_id": 1001, "values": [{"value": r"\\ip\orders\33860843"}]},
            {"field_id": 1002, "values": [{"value": "4"}]},
            {"field_id": 1003, "values": [{"value": "33860843"}]},
        ],
    }
    FakeAmoClient.updates = []
    monkeypatch.setattr(main_module, "AmoClient", FakeAmoClient)

    response = client.post(
        f"/webhooks/amocrm/{setup['webhook_secret']}",
        data=official_webhook(),
    )
    assert response.status_code == 202
    assert response.json()["accepted"] is True
    with session_factory() as session:
        assert session.query(ProofJob).count() == 1
    assert len(FakeAmoClient.updates) == 1


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

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Для webhook amoCRM настройте ровно три поля и поля для очистки."
    }
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


def test_amocrm_file_is_linked_to_lead_through_files_api() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(204)
        if request.method == "PUT":
            return httpx.Response(202)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with main_module.AmoClient(
            "https://company.amocrm.ru",
            "secret-token",
            timeout_seconds=8,
            client=http_client,
        ) as amo:
            assert not amo.is_file_linked("7654321", "file-uuid")
            amo.link_file("7654321", "file-uuid")

    assert requests[0].url.path == "/api/v4/leads/7654321/files"
    assert requests[1].url.path == "/api/v4/leads/7654321/files"
    assert json.loads(requests[1].content) == [{"file_uuid": "file-uuid"}]


def test_amocrm_common_note_contains_only_yandex_file_link() -> None:
    requests: list[httpx.Request] = []
    link = "https://disk.yandex.ru/d/public-proof"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"_embedded": {"notes": []}})
        if request.method == "POST":
            return httpx.Response(200, json={"_embedded": {"notes": [{"id": 701}]}})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with main_module.AmoClient(
            "https://company.amocrm.ru",
            "secret-token",
            timeout_seconds=8,
            client=http_client,
        ) as amo:
            assert amo.find_common_note("7654321", link) is None
            assert amo.create_common_note("7654321", link) == {"id": 701}

    assert requests[0].url.path == "/api/v4/leads/7654321/notes"
    assert requests[0].url.params["filter[note_type]"] == "common"
    assert requests[0].url.params["limit"] == "100"
    assert json.loads(requests[1].content) == [{
        "note_type": "common",
        "params": {"text": link},
    }]


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


def test_legacy_attachment_setting_uses_yandex_delivery_without_duplicate_note(
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
        credentials = services_module.amocrm_credentials(integration, settings)
        credentials["yandex_disk_token"] = "yandex-secret"
        integration.credentials_encrypted = encrypt_secret(
            credential_cipher_for_settings(settings), credentials
        )
        integration.delivery_url = ""
        session.commit()

    FakeYandexDiskClient.upload_calls = 0
    FakeYandexDiskClient.uploaded_bytes = b""
    FakeYandexDiskClient.uploaded_path = ""
    FakeYandexAmoClient.create_note_calls = 0
    FakeYandexAmoClient.fail_note_once = True
    FakeYandexAmoClient.remote_note_exists = False
    FakeYandexAmoClient.updates = []
    monkeypatch.setattr(services_module, "YandexDiskClient", FakeYandexDiskClient)
    monkeypatch.setattr(services_module, "AmoClient", FakeYandexAmoClient)

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
        data={"metadata_json": json.dumps({
            "published_filename": "ЦП Макет 3 60х30.jpg",
            "published_revision": 4,
        })},
        files={"file": ("proof.jpg", b"proof-image", "image/jpeg")},
    )

    first = client.post(f"/api/v1/jobs/{job_id}/complete", headers=headers)
    assert first.status_code == 200
    assert first.json()["delivery_status"] == "failed"
    with session_factory() as session:
        delivery = session.query(ProofResultDelivery).one()
        assert delivery.yandex_uploaded_at is not None
        assert delivery.yandex_public_url == "https://disk.yandex.ru/d/proof-archive"

    retry = client.post(f"/api/v1/jobs/{job_id}/complete", headers=headers)
    assert retry.status_code == 200
    assert retry.json()["delivery_status"] == "delivered"

    assert FakeYandexDiskClient.upload_calls == 1
    assert FakeYandexAmoClient.create_note_calls == 1
    with zipfile.ZipFile(io.BytesIO(FakeYandexDiskClient.uploaded_bytes)) as archive:
        assert archive.namelist() == ["ЦП Макет 3 60х30.jpg"]
        assert archive.read("ЦП Макет 3 60х30.jpg") == b"proof-image"
    assert FakeYandexDiskClient.uploaded_path == "disk:/amoCRM/Сделки/ORDER-42/ORDER-42_4.zip"
    assert FakeYandexAmoClient.updates == [(
        "7654321",
        {
            "status_id": 90,
            "pipeline_id": 77,
        },
    )]
    with session_factory() as session:
        job = session.get(ProofJob, job_id)
        delivery = session.query(ProofResultDelivery).one()
        assert job.processing_status == "completed"
        assert job.delivery_status == "delivered"
        assert delivery.finalized_at is not None


def test_yandex_delivery_uploads_zip_once_and_recovers_note_without_duplication(
    client: TestClient,
    monkeypatch,
) -> None:
    setup = bootstrap()
    configuration = configured_amocrm().model_copy(update={
        "completed_status_id": 90,
        "delivery_mode": "yandex_disk_note",
        "yandex_disk_root": "Производство/Цветопробы",
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
        credentials = services_module.amocrm_credentials(integration, settings)
        credentials["yandex_disk_token"] = "yandex-secret"
        integration.credentials_encrypted = encrypt_secret(
            credential_cipher_for_settings(settings), credentials
        )
        integration.delivery_url = ""
        session.commit()

    FakeYandexDiskClient.upload_calls = 0
    FakeYandexDiskClient.uploaded_bytes = b""
    FakeYandexDiskClient.uploaded_path = ""
    FakeYandexAmoClient.create_note_calls = 0
    FakeYandexAmoClient.fail_note_once = True
    FakeYandexAmoClient.remote_note_exists = False
    FakeYandexAmoClient.updates = []
    monkeypatch.setattr(services_module, "YandexDiskClient", FakeYandexDiskClient)
    monkeypatch.setattr(services_module, "AmoClient", FakeYandexAmoClient)

    job_response = client.post(
        f"/webhooks/amocrm/{setup['webhook_secret']}",
        json={
            "event_id": "yandex-delivery",
            "event_type": "proof.requested",
            "crm_entity_type": "leads",
            "crm_entity_id": "7654321",
            "crm_order_id": "31095815",
            "input": {"source_path": "orders/31095815", "layout_number": 3},
        },
    )
    job_id = job_response.json()["job_id"]
    headers = {"Authorization": f"Bearer {setup['worker_token']}"}
    client.post("/api/v1/jobs/claim", headers=headers)
    client.post(f"/api/v1/jobs/{job_id}/start", headers=headers)
    client.post(
        f"/api/v1/jobs/{job_id}/result",
        headers={**headers, "Idempotency-Key": "yandex-result"},
        data={"metadata_json": json.dumps({
            "published_filename": "ЦП Макет 3 60х30.jpg",
            "published_revision": 4,
        })},
        files={"file": ("proof.jpg", b"proof-image", "image/jpeg")},
    )

    first = client.post(f"/api/v1/jobs/{job_id}/complete", headers=headers)
    assert first.json()["delivery_status"] == "failed"
    with session_factory() as session:
        failure = session.query(ProofEvent).filter_by(
            job_id=job_id, event_type="delivery.failed"
        ).one()
        assert failure.error_code == "AMOCRM_NOTE"
        assert "Добавление ссылки в примечание сделки amoCRM" in failure.message
        assert "temporary note failure" in failure.message
        assert json_load(failure.details_json, {})["stage"] == "AMOCRM_NOTE"
    retry = client.post(f"/api/v1/jobs/{job_id}/complete", headers=headers)
    assert retry.json()["delivery_status"] == "delivered"

    assert FakeYandexDiskClient.upload_calls == 1
    assert FakeYandexDiskClient.uploaded_path == (
        "disk:/amoCRM/Сделки/31095815/31095815_4.zip"
    )
    with zipfile.ZipFile(io.BytesIO(FakeYandexDiskClient.uploaded_bytes)) as archive:
        assert archive.namelist() == ["ЦП Макет 3 60х30.jpg"]
        assert archive.read("ЦП Макет 3 60х30.jpg") == b"proof-image"
    assert FakeYandexAmoClient.create_note_calls == 1
    assert FakeYandexAmoClient.updates == [(
        "7654321",
        {"status_id": 90, "pipeline_id": 77},
    )]
    with session_factory() as session:
        delivery = session.query(ProofResultDelivery).one()
        assert delivery.yandex_public_url == "https://disk.yandex.ru/d/proof-archive"
        assert delivery.amo_note_id == 701
        assert delivery.finalized_at is not None


def test_oauth_settings_start_and_callback_store_rotating_tokens_encrypted(
    client: TestClient,
    identity_headers: dict[str, str],
    monkeypatch,
) -> None:
    setup = bootstrap()
    settings_response = client.post(
        "/integrations/amocrm/oauth/settings",
        headers=identity_headers,
        data={
            "csrf_token": "proof-csrf",
            "api_base_url": "https://company.amocrm.ru",
            "api_timeout_seconds": "8",
            "client_id": "oauth-client-id",
            "client_secret": "oauth-client-secret",
        },
    )
    assert settings_response.status_code == 200
    assert "https://one.customcraft-mes.ru/proof/integrations/amocrm/oauth/callback" in settings_response.text

    start = client.post(
        "/integrations/amocrm/oauth/start",
        headers=identity_headers,
        data={"csrf_token": "proof-csrf"},
        follow_redirects=False,
    )
    assert start.status_code == 303
    location = start.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://www.amocrm.ru/oauth"
    assert query["client_id"] == ["oauth-client-id"]
    state = query["state"][0]
    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        assert integration.oauth_state_digest == token_digest(state)
        assert state not in integration.oauth_state_digest

    monkeypatch.setattr(
        main_module,
        "exchange_amocrm_oauth_token",
        lambda *_args, **_kwargs: AmoOAuthTokenSet(
            token_type="Bearer",
            expires_in=86400,
            server_time=int(time.time()),
            access_token="oauth-access-token",
            refresh_token="oauth-refresh-token",
        ),
    )
    monkeypatch.setattr(main_module, "AmoClient", FakeOAuthAccountClient)
    callback = client.get(
        "/integrations/amocrm/oauth/callback",
        params={
            "state": state,
            "code": "short-lived-code",
            "referer": "company.amocrm.ru",
        },
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/proof/integrations?amocrm=connected"
    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        credentials = services_module.amocrm_credentials(integration, settings)
        configuration = AmoIntegrationConfiguration.model_validate(
            json_load(integration.configuration_json, {})
        )
        assert credentials["access_token"] == "oauth-access-token"
        assert credentials["refresh_token"] == "oauth-refresh-token"
        assert "oauth-access-token" not in integration.credentials_encrypted
        assert configuration.account_id == 1234
        assert configuration.account_name == "CustomCraft"
        assert integration.oauth_state_digest == ""


def test_oauth_callback_rejects_unknown_state(client: TestClient) -> None:
    response = client.get(
        "/integrations/amocrm/oauth/callback",
        params={"state": "unknown", "code": "code", "referer": "company.amocrm.ru"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_public_amocrm_oauth_endpoints_are_discoverable_without_secrets(
    client: TestClient,
) -> None:
    assert client.get("/integrations/amocrm/oauth/callback").json() == {"status": "ready"}
    assert client.get("/integrations/amocrm/oauth/revoked").json() == {"status": "ready"}


def test_expired_oauth_access_token_is_refreshed_and_replacement_is_persisted() -> None:
    setup = bootstrap()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "token_type": "Bearer",
            "expires_in": 86400,
            "server_time": int(time.time()),
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
        })

    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        integration.configuration_json = json_dump(configured_amocrm().model_dump(mode="json"))
        integration.credentials_encrypted = encrypt_secret(
            credential_cipher_for_settings(settings),
            {
                "client_id": "oauth-client-id",
                "client_secret": "oauth-client-secret",
                "access_token": "expired-access-token",
                "refresh_token": "old-refresh-token",
                "expires_at": int(time.time()) - 10,
            },
        )
        session.commit()
        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            token = services_module.get_amocrm_access_token(
                session, settings, integration, client=http_client
            )
        session.refresh(integration)
        credentials = services_module.amocrm_credentials(integration, settings)
        assert token == "new-access-token"
        assert credentials["refresh_token"] == "new-refresh-token"
        assert session.query(ProofEvent).filter_by(
            event_type="amocrm.oauth.refreshed"
        ).count() == 1

    assert len(requests) == 1
    assert requests[0].url == "https://company.amocrm.ru/oauth2/access_token"
    payload = json.loads(requests[0].content)
    assert payload["grant_type"] == "refresh_token"
    assert payload["refresh_token"] == "old-refresh-token"
    assert payload["redirect_uri"] == (
        "https://one.customcraft-mes.ru/proof/integrations/amocrm/oauth/callback"
    )


def test_signed_amocrm_revocation_hook_disconnects_the_matching_account(
    client: TestClient,
) -> None:
    setup = bootstrap()
    client_id = "oauth-client-id"
    client_secret = "oauth-client-secret"
    account_id = "1234"
    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        configuration = configured_amocrm().model_copy(update={
            "account_id": int(account_id),
            "account_name": "CustomCraft",
            "connected_at": "2026-09-09T12:00:00",
        })
        integration.configuration_json = json_dump(configuration.model_dump(mode="json"))
        integration.credentials_encrypted = encrypt_secret(
            credential_cipher_for_settings(settings),
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_at": int(time.time()) + 3600,
            },
        )
        session.commit()
    signature = hmac.new(
        client_secret.encode(),
        f"{client_id}|{account_id}".encode(),
        hashlib.sha256,
    ).hexdigest()

    response = client.get(
        "/integrations/amocrm/oauth/revoked",
        params={
            "client_uuid": client_id,
            "account_id": account_id,
            "signature": signature,
        },
    )
    assert response.status_code == 200
    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        credentials = services_module.amocrm_credentials(integration, settings)
        configuration = AmoIntegrationConfiguration.model_validate(
            json_load(integration.configuration_json, {})
        )
        assert credentials == {
            "client_id": client_id,
            "client_secret": client_secret,
        }
        assert integration.enabled is False
        assert configuration.account_id is None
