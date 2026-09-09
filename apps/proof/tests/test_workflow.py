from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import parse_amocrm_form_payload, session_factory
from app.models import ProofEvent, ProofIntegration, ProofJob, ProofResult, ProofWorker, utc_now
from app.services import register_worker
from tests.helpers import bootstrap, webhook_payload, worker_headers


def create_job(client: TestClient, setup: dict[str, object]) -> str:
    response = client.post(
        f"/webhooks/amocrm/{setup['webhook_secret']}",
        json=webhook_payload(int(setup["preset_id"])),
    )
    assert response.status_code == 202
    return response.json()["job_id"]


def test_end_to_end_mock_worker_and_delivery(client: TestClient) -> None:
    setup = bootstrap()
    job_id = create_job(client, setup)
    headers = worker_headers(str(setup["worker_token"]))

    heartbeat = client.post(
        "/api/v1/workers/heartbeat",
        headers=headers,
        json={"hostname": "proof-node-01", "version": "1.0.0", "availability": "available", "capabilities": ["proof-v1"]},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["online"] is True

    claim = client.post("/api/v1/jobs/claim", headers=headers)
    assert claim.status_code == 200
    assert claim.json()["id"] == job_id
    assert claim.json()["preset"]["parameters"] == {}
    assert claim.json()["input"]["source_path"] == "orders/ORDER-42"
    assert claim.json()["input"]["layout_number"] == 3

    assert client.post(f"/api/v1/jobs/{job_id}/start", headers=headers).status_code == 200
    progress = client.post(
        f"/api/v1/jobs/{job_id}/progress", headers=headers,
        json={"progress": 55, "current_stage": "worker-reported-stage"},
    )
    assert progress.json() == {"job_id": job_id, "progress": 55, "current_stage": "worker-reported-stage"}
    event = client.post(
        f"/api/v1/jobs/{job_id}/events", headers=headers,
        json={"event_type": "worker.diagnostic", "level": "info", "message": "Source opened", "details": {"token": "must-not-leak"}},
    )
    assert event.status_code == 200

    result_bytes = b"\xff\xd8synthetic-proof-jpeg\xff\xd9"
    result_sha256 = hashlib.sha256(result_bytes).hexdigest()
    idempotency_key = hashlib.sha256(
        f"proof-worker-v1:{job_id}:1:{result_sha256}".encode()
    ).hexdigest()
    result_metadata = json.dumps({"sha256": result_sha256, "attempt": 1, "width": 120})
    upload = client.post(
        f"/api/v1/jobs/{job_id}/result",
        headers={**headers, "Idempotency-Key": idempotency_key},
        data={"metadata_json": result_metadata},
        files={"file": ("result.jpg", result_bytes, "image/jpeg")},
    )
    assert upload.status_code == 201
    assert upload.json()["sha256"] == result_sha256
    duplicate = client.post(
        f"/api/v1/jobs/{job_id}/result",
        headers={**headers, "Idempotency-Key": idempotency_key},
        data={"metadata_json": result_metadata},
        files={"file": ("result.jpg", result_bytes, "image/jpeg")},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    completed = client.post(f"/api/v1/jobs/{job_id}/complete", headers=headers)
    assert completed.status_code == 200
    assert completed.json()["processing_status"] == "completed"
    assert completed.json()["delivery_status"] == "delivered"

    with session_factory() as session:
        job = session.get(ProofJob, job_id)
        assert job.processing_status == "completed"
        assert job.delivery_status == "delivered"
        assert session.scalar(select(ProofResult).where(ProofResult.job_id == job_id)) is not None
        diagnostic = session.scalar(select(ProofEvent).where(ProofEvent.event_type == "worker.diagnostic"))
        assert "must-not-leak" not in diagnostic.details_json


def test_webhook_and_result_are_idempotent(client: TestClient) -> None:
    setup = bootstrap()
    first = client.post(f"/webhooks/amocrm/{setup['webhook_secret']}", json=webhook_payload())
    second = client.post(f"/webhooks/amocrm/{setup['webhook_secret']}", json=webhook_payload())
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {"accepted": True, "duplicate": True, "job_id": first.json()["job_id"]}
    with session_factory() as session:
        assert session.query(ProofJob).count() == 1


def test_official_amocrm_form_shape_is_accepted() -> None:
    payload = {
        "account[id]": "1234",
        "leads[status][0][id]": "7654321",
        "leads[status][0][last_modified]": "1788700000",
        "leads[status][0][status_id]": "999",
    }
    parsed = parse_amocrm_form_payload(payload)

    assert parsed.event_type == "leads.status"
    assert parsed.crm_entity_id == "7654321"
    expected_source = json.dumps(
        sorted(payload.items()), ensure_ascii=False, separators=(",", ":")
    )
    assert parsed.event_id == hashlib.sha256(expected_source.encode()).hexdigest()


def test_official_amocrm_event_id_covers_the_whole_form_payload() -> None:
    first = {
        "account[id]": "1234",
        "leads[status][0][id]": "7654321",
        "leads[status][0][last_modified]": "1788700000",
        "leads[status][0][status_id]": "88",
    }
    second = {**first, "leads[status][0][status_id]": "89"}

    assert parse_amocrm_form_payload(first).event_id != parse_amocrm_form_payload(second).event_id


def test_processing_failure_and_delivery_failure_are_independent(client: TestClient) -> None:
    setup = bootstrap(delivery_url="")
    job_id = create_job(client, setup)
    headers = worker_headers(str(setup["worker_token"]))
    client.post("/api/v1/jobs/claim", headers=headers)
    client.post(f"/api/v1/jobs/{job_id}/start", headers=headers)
    client.post(
        f"/api/v1/jobs/{job_id}/result",
        headers={**headers, "Idempotency-Key": "delivery-failure-result"},
        files={"file": ("proof.bin", b"result", "application/octet-stream")},
    )
    completed = client.post(f"/api/v1/jobs/{job_id}/complete", headers=headers)
    assert completed.json()["processing_status"] == "completed"
    assert completed.json()["delivery_status"] == "failed"

    with session_factory() as session:
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        integration.delivery_url = "mock://delivered"
        session.commit()
    retried = client.post(
        f"/jobs/{job_id}/retry-delivery",
        headers={"X-Bellenne-User-Id": "17", "X-Bellenne-Username": "proof-owner", "X-Bellenne-Csrf-Token": "proof-csrf"},
        data={"csrf_token": "proof-csrf"},
        follow_redirects=False,
    )
    assert retried.status_code == 303
    with session_factory() as session:
        job = session.get(ProofJob, job_id)
        assert job.processing_status == "completed"
        assert job.delivery_status == "delivered"
        assert job.attempt == 1


def test_stale_worker_job_is_requeued_for_another_worker(client: TestClient) -> None:
    setup = bootstrap()
    job_id = create_job(client, setup)
    first_headers = worker_headers(str(setup["worker_token"]))
    client.post("/api/v1/jobs/claim", headers=first_headers)
    client.post(f"/api/v1/jobs/{job_id}/start", headers=first_headers)

    with session_factory() as session:
        first_worker = session.get(ProofWorker, str(setup["worker_id"]))
        first_worker.last_heartbeat_at = utc_now() - timedelta(seconds=first_worker.heartbeat_timeout_seconds + 1)
        second_worker, second_token = register_worker(session, 17, "WORKER-02", 60)
        session.commit()
        second_id = second_worker.id
    claim = client.post("/api/v1/jobs/claim", headers=worker_headers(second_token))
    assert claim.status_code == 200
    assert claim.json()["id"] == job_id
    with session_factory() as session:
        job = session.get(ProofJob, job_id)
        assert job.worker_id == second_id
        assert job.processing_status == "assigned"


def test_worker_failure_redacts_secrets_and_can_retry(
    client: TestClient, identity_headers: dict[str, str]
) -> None:
    setup = bootstrap()
    job_id = create_job(client, setup)
    headers = worker_headers(str(setup["worker_token"]))
    client.post("/api/v1/jobs/claim", headers=headers)
    client.post(f"/api/v1/jobs/{job_id}/start", headers=headers)
    failed = client.post(
        f"/api/v1/jobs/{job_id}/fail", headers=headers,
        json={"error_code": "SOURCE_NOT_FOUND", "message": "Failed; Authorization: Bearer secret-value", "details": {"password": "hidden"}},
    )
    assert failed.status_code == 200
    assert failed.json()["processing_status"] == "failed"
    retry = client.post(
        f"/jobs/{job_id}/retry", headers=identity_headers,
        data={"csrf_token": "proof-csrf"}, follow_redirects=False,
    )
    assert retry.status_code == 303
    with session_factory() as session:
        job = session.get(ProofJob, job_id)
        assert job.processing_status == "queued"
        assert "secret-value" not in (job.error_message or "")
