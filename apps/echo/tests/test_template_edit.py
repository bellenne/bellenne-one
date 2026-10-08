import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import ReplyTemplate, User


class TemplateEditTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        with self.sessions() as db:
            db.add_all([
                User(id=1, username="owner", password_hash="test"),
                User(id=2, username="other", password_hash="test"),
                ReplyTemplate(
                    id=10, user_id=1, name="Старый шаблон", match_mode="article",
                    match_value="ART-100", review_kind="photo", rating_from=2,
                    rating_to=4, body="Старый ответ {товар}", is_active=False,
                    created_at=datetime(2026, 1, 1),
                ),
            ])
            db.commit()

        def session_override():
            with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = session_override
        self.auth_patch = patch(
            "app.main.require_user",
            lambda request, db: db.get(User, int(request.headers["X-Test-User"])),
        )
        self.auth_patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.auth_patch.stop()
        app.dependency_overrides.clear()
        self.engine.dispose()

    def test_edit_form_is_prefilled_and_owned(self):
        response = self.client.get("/templates?edit=10", headers={"X-Test-User": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/templates/10/edit"', response.text)
        self.assertIn('value="Старый шаблон"', response.text)
        self.assertIn('value="ART-100"', response.text)
        self.assertIn("Старый ответ {товар}", response.text)
        self.assertIn("Сохранить изменения", response.text)
        self.assertEqual(
            self.client.get("/templates?edit=10", headers={"X-Test-User": "2"}).status_code,
            404,
        )

    def test_owner_can_update_without_replacing_template_or_active_state(self):
        response = self.client.post(
            "/templates/10/edit", headers={"X-Test-User": "1"},
            data={
                "name": "Новый шаблон", "match_mode": "all", "match_value": "ignored",
                "review_kind": "text", "rating_from": "5", "rating_to": "3",
                "body": "Новый ответ {оценка}",
            }, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with self.sessions() as db:
            item = db.get(ReplyTemplate, 10)
            self.assertEqual(item.user_id, 1)
            self.assertEqual(item.name, "Новый шаблон")
            self.assertEqual((item.match_mode, item.match_value), ("all", ""))
            self.assertEqual(item.review_kind, "text")
            self.assertEqual((item.rating_from, item.rating_to), (3, 5))
            self.assertEqual(item.body, "Новый ответ {оценка}")
            self.assertFalse(item.is_active)
            self.assertEqual(item.created_at, datetime(2026, 1, 1))

    def test_other_user_cannot_update_or_view_template(self):
        response = self.client.post(
            "/templates/10/edit", headers={"X-Test-User": "2"},
            data={"name": "Чужой", "body": "Чужой ответ"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 404)
        with self.sessions() as db:
            self.assertEqual(db.get(ReplyTemplate, 10).name, "Старый шаблон")


if __name__ == "__main__":
    unittest.main()
