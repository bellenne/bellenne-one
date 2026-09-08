from __future__ import annotations

from app.main import session_factory
from app.models import ProofIntegration
from app.security import encrypt_secret, new_secret, secret_parts, token_digest
from app.services import create_preset, credential_cipher_for_settings, register_worker
from app.main import settings


def bootstrap(owner_id: int = 17, *, delivery_url: str = "mock://delivered") -> dict[str, object]:
    with session_factory() as session:
        preset = create_preset(session, owner_id, "Production", {"contract": "worker-v1"})
        worker, worker_token = register_worker(session, owner_id, "WORKER-01", 60)
        webhook_secret = new_secret("proof_hook")
        prefix, last_four = secret_parts(webhook_secret)
        integration = ProofIntegration(
            owner_external_user_id=owner_id,
            kind="amocrm",
            enabled=True,
            trigger_events_json='["proof.requested","leads.status"]',
            default_preset_id=preset.id,
            delivery_url=delivery_url,
            credentials_encrypted=encrypt_secret(
                credential_cipher_for_settings(settings), {"access_token": "test-access-token"}
            ),
            webhook_secret_digest=token_digest(webhook_secret),
            webhook_secret_prefix=prefix,
            webhook_secret_last_four=last_four,
        )
        session.add(integration)
        session.commit()
        return {
            "preset_id": preset.id,
            "worker_id": worker.id,
            "worker_token": worker_token,
            "webhook_secret": webhook_secret,
            "integration_id": integration.id,
        }


def worker_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def webhook_payload(preset_id: int | None = None) -> dict[str, object]:
    return {
        "event_id": "amo-event-001",
        "event_type": "proof.requested",
        "crm_entity_type": "leads",
        "crm_entity_id": "7654321",
        "crm_order_id": "ORDER-42",
        "preset_id": preset_id,
        "input": {"source_file": "design.tif"},
    }
