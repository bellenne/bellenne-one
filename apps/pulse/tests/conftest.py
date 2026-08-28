from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import User
from sqlalchemy import select


@pytest.fixture(scope="session")
def client(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("bellennepulse")
    settings = Settings(
        database_url=f"sqlite:///{(data_dir / 'test.db').as_posix()}",
        scheduler_enabled=False,
        app_secret_key="test-secret-key",
        demo_mode=True,
        seed_demo_data=True,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token is missing"
    return match.group(1)


@pytest.fixture()
def authenticated_client(client):
    client.cookies.clear()
    with client.app.state.session_factory() as session:
        user_exists = session.scalar(select(User).where(User.username == "test-owner")) is not None
    if user_exists:
        page = client.get("/login")
        response = client.post(
            "/login",
            data={"csrf_token": csrf_from(page.text), "username": "test-owner", "password": "test-password"},
            follow_redirects=False,
        )
    else:
        page = client.get("/register")
        response = client.post(
            "/register",
            data={
                "csrf_token": csrf_from(page.text),
                "username": "test-owner",
                "password": "test-password",
                "password_confirm": "test-password",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    yield client
