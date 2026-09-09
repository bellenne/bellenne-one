from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="bellenneproof-tests-"))
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ["PROOF_RESULT_DIR"] = str(TEST_DATA_DIR / "results")
os.environ["DATABASE_URL"] = f"sqlite:///{(TEST_DATA_DIR / 'test.db').as_posix()}"
os.environ["MODULE_PREFIX"] = "/proof"
os.environ["APP_SECRET_KEY"] = "proof-test-secret"
os.environ["PROOF_NOTIFICATION_ASYNC"] = "false"
os.environ["PROOF_PUBLIC_BASE_URL"] = "https://one.customcraft-mes.ru"

from app.main import app, session_factory  # noqa: E402
from app.models import (  # noqa: E402
    ProofEvent,
    ProofIntegration,
    ProofJob,
    ProofNotificationDelivery,
    ProofPreset,
    ProofResult,
    ProofResultDelivery,
    ProofWorker,
    WebhookReceipt,
)


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        with session_factory() as session:
            for model in (ProofNotificationDelivery, ProofEvent, ProofResultDelivery, ProofResult, WebhookReceipt, ProofJob, ProofWorker, ProofIntegration, ProofPreset):
                session.query(model).delete()
            session.commit()
        shutil.rmtree(TEST_DATA_DIR / "results", ignore_errors=True)
        (TEST_DATA_DIR / "results").mkdir(parents=True, exist_ok=True)
        yield test_client


@pytest.fixture()
def identity_headers() -> dict[str, str]:
    return {
        "X-Bellenne-User-Id": "17",
        "X-Bellenne-Username": "proof-owner",
        "X-Bellenne-Csrf-Token": "proof-csrf",
    }


def pytest_sessionfinish(session, exitstatus):
    del session, exitstatus
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
