from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


TEST_TOKEN = "test-nest-token-with-at-least-32-characters"
TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="bellennenest-tests-"))
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ["DATABASE_URL"] = f"sqlite:///{(TEST_DATA_DIR / 'test.db').as_posix()}"
os.environ["MODULE_PREFIX"] = "/nest"
from app.main import app, session_factory  # noqa: E402
from app.models import ApiToken  # noqa: E402
from app.security import token_digest, token_parts  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        prefix, last_four = token_parts(TEST_TOKEN)
        with session_factory() as session:
            credential = session.query(ApiToken).filter_by(external_user_id=17).one_or_none()
            if credential is None:
                credential = ApiToken(
                    external_user_id=17,
                    owner_username="nest-owner",
                    token_digest=token_digest(TEST_TOKEN),
                    token_prefix=prefix,
                    token_last_four=last_four,
                )
                session.add(credential)
            else:
                credential.owner_username = "nest-owner"
                credential.token_digest = token_digest(TEST_TOKEN)
                credential.token_prefix = prefix
                credential.token_last_four = last_four
                credential.last_used_at = None
            session.commit()
        yield test_client


@pytest.fixture()
def identity_headers() -> dict[str, str]:
    return {
        "X-Bellenne-User-Id": "17",
        "X-Bellenne-Username": "nest-owner",
        "X-Bellenne-Csrf-Token": "test-csrf-token",
    }


def pytest_sessionfinish(session, exitstatus):
    del session, exitstatus
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
