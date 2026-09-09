from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import session_factory, settings
from app.models import ProofEvent, ProofIntegration, ProofJob, ProofPreset, ProofResult, ProofWorker
from app.services import (
    claim_next_job,
    deliver_result,
    dispatch_worker_wakeups,
    revise_preset,
    transition_processing,
    update_worker_configuration,
)
from tests.helpers import bootstrap, webhook_payload, worker_headers


def worker_settings_form() -> dict[str, str]:
    return {
        "csrf_token": "proof-csrf",
        "callback_url": "http://proof-worker:8090/wake",
        "unc_prefix": r"\\10.0.0.8\дизайн отдел",
        "local_root": "/sources/main",
        "heartbeat_interval": "20",
        "poll_interval": "4",
        "retry_initial_seconds": "2",
        "retry_max_seconds": "40",
        "storage_retry_limit": "7",
        "file_not_found_retry_limit": "1",
        "health_interval": "6",
        "health_max_age": "100",
        "wake_timeout_seconds": "3",
    }


def test_worker_configuration_is_changed_in_ui_and_returned_by_heartbeat(
    client: TestClient, identity_headers: dict[str, str]
) -> None:
    setup = bootstrap()
    response = client.post(
        f"/workers/{setup['worker_id']}/settings",
        headers=identity_headers,
        data=worker_settings_form(),
        follow_redirects=False,
    )
    assert response.status_code == 303

    heartbeat = client.post(
        "/api/v1/workers/heartbeat",
        headers=worker_headers(str(setup["worker_token"])),
        json={
            "hostname": "proof-worker",
            "version": "0.1.0",
            "availability": "available",
            "capabilities": ["proof_render"],
        },
    )
    assert heartbeat.status_code == 200
    payload = heartbeat.json()
    assert payload["configuration_version"] == 2
    assert payload["configuration"]["poll_interval"] == 4
    assert payload["configuration"]["path_mappings"] == [{
        "source_prefix": r"\\10.0.0.8\дизайн отдел",
        "local_root": "/sources/main",
    }]


def test_core_sends_signed_worker_wake_webhook(client: TestClient) -> None:
    del client
    setup = bootstrap()
    with session_factory() as session:
        worker = session.get(ProofWorker, str(setup["worker_id"]))
        update_worker_configuration(worker, **{
            key: (float(value) if key in {
                "heartbeat_interval", "poll_interval", "retry_initial_seconds",
                "retry_max_seconds", "health_interval", "health_max_age", "wake_timeout_seconds",
            } else int(value) if key in {"storage_retry_limit", "file_not_found_retry_limit"} else value)
            for key, value in worker_settings_form().items() if key != "csrf_token"
        })
        token_hash = worker.token_digest
        session.commit()

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        timestamp = request.headers["X-Proof-Timestamp"]
        captured["payload"] = json.loads(body)
        captured["signature"] = request.headers["X-Proof-Signature"]
        captured["expected"] = hmac.new(
            bytes.fromhex(token_hash), timestamp.encode("ascii") + b"." + body, hashlib.sha256
        ).hexdigest()
        return httpx.Response(202, json={"accepted": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        assert dispatch_worker_wakeups(
            session_factory, 17, "job-for-wake", client=http_client
        ) == 1
    assert captured["signature"] == captured["expected"]
    assert captured["payload"] == {"event": "queue.changed", "job_id": "job-for-wake"}


def test_atomic_claim_assigns_job_to_only_one_worker(client: TestClient) -> None:
    setup = bootstrap()
    webhook = client.post(f"/webhooks/amocrm/{setup['webhook_secret']}", json=webhook_payload())
    job_id = webhook.json()["job_id"]
    with session_factory() as session:
        from app.services import register_worker
        second, _ = register_worker(session, 17, "WORKER-02", 60)
        session.commit()
        worker_ids = [str(setup["worker_id"]), second.id]

    barrier = Barrier(2)

    def claim(worker_id: str) -> str | None:
        with session_factory() as session:
            worker = session.get(ProofWorker, worker_id)
            barrier.wait()
            job = claim_next_job(session, worker)
            session.commit()
            return job.id if job else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, worker_ids))
    assert claimed.count(job_id) == 1
    assert claimed.count(None) == 1


def test_concurrent_webhook_delivery_creates_one_job(client: TestClient) -> None:
    setup = bootstrap()
    barrier = Barrier(2)

    def deliver(_: int) -> tuple[int, dict[str, object]]:
        barrier.wait()
        response = client.post(
            f"/webhooks/amocrm/{setup['webhook_secret']}", json=webhook_payload()
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(deliver, range(2)))
    assert sorted(status_code for status_code, _ in responses) == [200, 202]
    assert sum(1 for _, body in responses if body["duplicate"] is True) == 1
    assert len({body["job_id"] for _, body in responses}) == 1
    with session_factory() as session:
        assert session.query(ProofJob).count() == 1


def test_concurrent_result_upload_is_idempotent(client: TestClient) -> None:
    setup = bootstrap()
    webhook = client.post(f"/webhooks/amocrm/{setup['webhook_secret']}", json=webhook_payload())
    job_id = webhook.json()["job_id"]
    headers = worker_headers(str(setup["worker_token"]))
    client.post("/api/v1/jobs/claim", headers=headers)
    client.post(f"/api/v1/jobs/{job_id}/start", headers=headers)
    barrier = Barrier(2)

    def upload(_: int) -> tuple[int, dict[str, object]]:
        barrier.wait()
        response = client.post(
            f"/api/v1/jobs/{job_id}/result",
            headers={**headers, "Idempotency-Key": "same-upload"},
            files={"file": ("proof.bin", b"same-content", "application/octet-stream")},
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(upload, range(2)))
    assert sorted(status_code for status_code, _ in responses) == [200, 201]
    assert sum(1 for _, body in responses if body["duplicate"] is True) == 1
    assert len({body["result_id"] for _, body in responses}) == 1
    with session_factory() as session:
        assert session.query(ProofResult).count() == 1


def test_preset_revision_preserves_old_job_snapshot_and_updates_default(client: TestClient) -> None:
    setup = bootstrap()
    first = client.post(f"/webhooks/amocrm/{setup['webhook_secret']}", json=webhook_payload()).json()
    with session_factory() as session:
        current = session.get(ProofPreset, int(setup["preset_id"]))
        revised = revise_preset(session, current, "Production", {"output_dpi": 96})
        session.commit()
        revised_id = revised.id
    second_payload = webhook_payload()
    second_payload["event_id"] = "amo-event-002"
    second = client.post(f"/webhooks/amocrm/{setup['webhook_secret']}", json=second_payload).json()
    with session_factory() as session:
        first_job = session.get(ProofJob, first["job_id"])
        second_job = session.get(ProofJob, second["job_id"])
        integration = session.get(ProofIntegration, int(setup["integration_id"]))
        assert first_job.preset_version == 1
        assert first_job.preset_snapshot_json == '{}'
        assert second_job.preset_version == 2
        assert second_job.preset_snapshot_json == '{"output_dpi":96}'
        assert integration.default_preset_id == revised_id


def test_retry_job_preserves_previous_result_and_creates_new_attempt(
    client: TestClient, identity_headers: dict[str, str]
) -> None:
    setup = bootstrap()
    webhook = client.post(f"/webhooks/amocrm/{setup['webhook_secret']}", json=webhook_payload())
    job_id = webhook.json()["job_id"]
    headers = worker_headers(str(setup["worker_token"]))
    client.post("/api/v1/jobs/claim", headers=headers)
    client.post(f"/api/v1/jobs/{job_id}/start", headers=headers)
    client.post(
        f"/api/v1/jobs/{job_id}/result",
        headers={**headers, "Idempotency-Key": "first-attempt"},
        files={"file": ("first.bin", b"first", "application/octet-stream")},
    )
    client.post(f"/api/v1/jobs/{job_id}/complete", headers=headers)
    response = client.post(
        f"/jobs/{job_id}/retry", headers=identity_headers,
        data={"csrf_token": "proof-csrf"}, follow_redirects=False,
    )
    assert response.status_code == 303
    with session_factory() as session:
        job = session.get(ProofJob, job_id)
        assert job.processing_status == "queued"
        assert job.delivery_status == "pending"
        assert job.attempt == 2
        assert job.current_result_id is None
        results = list(session.scalars(select(ProofResult).where(ProofResult.job_id == job_id)))
        assert len(results) == 1
        assert results[0].attempt == 1


def test_http_delivery_sends_file_without_exposing_amocrm_token(client: TestClient) -> None:
    setup = bootstrap(delivery_url="https://adapter.invalid/proof-result")
    webhook = client.post(f"/webhooks/amocrm/{setup['webhook_secret']}", json=webhook_payload())
    job_id = webhook.json()["job_id"]
    headers = worker_headers(str(setup["worker_token"]))
    client.post("/api/v1/jobs/claim", headers=headers)
    client.post(f"/api/v1/jobs/{job_id}/start", headers=headers)
    client.post(
        f"/api/v1/jobs/{job_id}/result",
        headers={**headers, "Idempotency-Key": "http-delivery"},
        files={"file": ("proof.bin", b"proof-content", "application/octet-stream")},
    )
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["content_type"] = request.headers.get("content-type")
        captured["body"] = request.read()
        return httpx.Response(200)

    with session_factory() as session:
        job = session.get(ProofJob, job_id)
        transition_processing(session, job, "completed", source="worker", message="Completed for delivery test.")
        job.completed_at = job.updated_at
        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            assert deliver_result(session, settings, job, client=http_client) is True
        session.commit()
        assert job.processing_status == "completed"
        assert job.delivery_status == "delivered"
        serialized_events = " ".join(event.message + event.details_json for event in session.scalars(select(ProofEvent)))
        assert "test-access-token" not in serialized_events
    assert captured["authorization"] is None
    assert str(captured["content_type"]).startswith("multipart/form-data;")
    assert b"proof-content" in captured["body"]
