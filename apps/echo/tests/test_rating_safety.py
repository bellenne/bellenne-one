import asyncio
import os
import random
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import log_unsafe_template_range, template_safety_warning
from app.marketplaces import (
    MarketplaceError,
    OzonClient,
    Review,
    ReviewFetchSnapshot,
    WildberriesClient,
    _has_real_answer,
    parse_rating,
)
from app.models import Cabinet, ProcessingQueueItem, ReplyLog, ReplyTemplate, User
from app.worker import _build_queue, _process_queue, choose_template, process_user


def template(name: str, rating_from: int, rating_to: int, body: str, user_id: int | None = None) -> ReplyTemplate:
    return ReplyTemplate(
        user_id=user_id,
        name=name,
        match_mode="all",
        match_value="",
        review_kind="any",
        rating_from=rating_from,
        rating_to=rating_to,
        body=body,
        created_at=datetime(2026, 1, rating_from),
    )


def template_with_created_at(
    name: str,
    rating_from: int,
    rating_to: int,
    body: str,
    created_at: datetime,
    user_id: int | None = None,
) -> ReplyTemplate:
    item = template(name, rating_from, rating_to, body, user_id)
    item.created_at = created_at
    return item


def article_template(
    name: str,
    rating_from: int,
    rating_to: int,
    body: str,
    article: str,
    created_at: datetime,
    user_id: int | None = None,
) -> ReplyTemplate:
    item = template_with_created_at(name, rating_from, rating_to, body, created_at, user_id)
    item.match_mode = "article"
    item.match_value = article
    return item


class RatingSafetyTests(unittest.TestCase):
    def test_parse_rating_accepts_only_one_to_five(self):
        self.assertEqual(parse_rating(1), 1)
        self.assertEqual(parse_rating("2"), 2)
        self.assertEqual(parse_rating("2.0"), 2)
        self.assertEqual(parse_rating("3,0"), 3)
        self.assertEqual(parse_rating(" 4.000 "), 4)
        self.assertEqual(parse_rating("2.7"), 0)
        self.assertEqual(parse_rating("4.0000000000000001"), 0)
        self.assertEqual(parse_rating("1e0"), 0)
        self.assertEqual(parse_rating("NaN"), 0)
        self.assertEqual(parse_rating("Infinity"), 0)
        self.assertEqual(parse_rating(["5"]), 0)
        self.assertEqual(parse_rating({"rating": 5}), 0)
        self.assertEqual(parse_rating(True), 0)
        self.assertEqual(parse_rating(None), 0)
        self.assertEqual(parse_rating(""), 0)
        self.assertEqual(parse_rating("bad"), 0)
        self.assertEqual(parse_rating(0), 0)
        self.assertEqual(parse_rating(6), 0)

    def test_wb_review_rating_uses_safe_parser(self):
        client = WildberriesClient(SimpleNamespace(wb_api_key="test"))

        valid = client._review_from_item({"id": "1", "productValuation": "2.0", "productDetails": {}})
        invalid = client._review_from_item({"id": "2", "productValuation": "bad", "productDetails": {}})

        self.assertEqual(valid.rating, 2)
        self.assertEqual(invalid.rating, 0)

    def test_wb_fetch_reviews_to_answer_preserves_limit(self):
        client = WildberriesClient(SimpleNamespace(wb_api_key="test"))
        seen_limits = []

        async def fake_snapshot(take):
            seen_limits.append(take)
            return ReviewFetchSnapshot(reviews=[], diagnostics="")

        client.fetch_queue_snapshot = fake_snapshot

        reviews = asyncio.run(client.fetch_reviews_to_answer(37))

        self.assertEqual(reviews, [])
        self.assertEqual(seen_limits, [37])

    def test_wb_fetch_review_by_id_parses_nested_feedback_payload(self):
        client = WildberriesClient(SimpleNamespace(wb_api_key="test"))

        class FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {
                    "data": {
                        "feedbacks": [
                            {
                                "id": "wb-review",
                                "productValuation": "1",
                                "productDetails": {
                                    "productName": "Товар",
                                    "supplierArticle": "ART-100",
                                },
                            }
                        ]
                    }
                }

        async def fake_request(_http_client, method, path, **kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/api/v1/feedback")
            self.assertEqual(kwargs["params"], {"id": "wb-review"})
            return FakeResponse()

        client._request = fake_request

        review = asyncio.run(client.fetch_review_by_id("wb-review"))

        self.assertIsNotNone(review)
        self.assertEqual(review.id, "wb-review")
        self.assertEqual(review.rating, 1)
        self.assertEqual(review.rating_raw, "1")
        self.assertEqual(review.rating_source, "productValuation")
        self.assertEqual(review.article, "ART-100")

    def test_wb_queue_snapshot_logs_answered_and_unanswered_counts(self):
        client = WildberriesClient(SimpleNamespace(wb_api_key="test"))

        class FakeResponse:
            status_code = 200
            text = ""

            def __init__(self, feedbacks):
                self._feedbacks = feedbacks

            def json(self):
                return {"data": {"feedbacks": self._feedbacks}}

        async def fake_request(_http_client, _method, _path, **kwargs):
            params = kwargs["params"]
            if params["isAnswered"] == "false":
                return FakeResponse(
                    [
                        {"id": "low", "productValuation": 1, "productDetails": {"productName": "Товар"}},
                        {"id": "high", "productValuation": 5, "productDetails": {"productName": "Товар"}},
                    ]
                )
            return FakeResponse(
                [
                    {
                        "id": "answered",
                        "productValuation": 5,
                        "answer": {"text": "Спасибо"},
                        "productDetails": {"productName": "Товар"},
                    },
                    {
                        "id": "answered-without-answer",
                        "productValuation": 2,
                        "answer": None,
                        "productDetails": {"productName": "Товар"},
                    },
                    {
                        "id": "answered-state-only",
                        "productValuation": 1,
                        "answer": {"text": "", "state": "wbRu"},
                        "productDetails": {"productName": "Товар"},
                    },
                ]
            )

        client._request = fake_request

        snapshot = asyncio.run(client.fetch_queue_snapshot(5000))

        self.assertIn("isAnswered=false 2", snapshot.diagnostics)
        self.assertIn("isAnswered=true 3", snapshot.diagnostics)
        self.assertIn("с answer 1", snapshot.diagnostics)
        self.assertIn("без answer 2", snapshot.diagnostics)
        self.assertEqual(
            [review.id for review in snapshot.reviews],
            ["low", "high", "answered-without-answer", "answered-state-only"],
        )

    def test_answer_state_without_text_is_not_counted_as_real_answer(self):
        self.assertFalse(_has_real_answer({"answer": {"text": "", "state": "wbRu"}}))
        self.assertFalse(_has_real_answer({"answer": {"state": "wbRu"}}))
        self.assertFalse(_has_real_answer({"reply_state": "wbRu"}))
        self.assertTrue(_has_real_answer({"answer": {"text": "Спасибо"}}))
        self.assertTrue(_has_real_answer({"replies": [{"text": "Спасибо"}]}))

    def test_ozon_rating_zero_is_not_replaced_by_score(self):
        client = OzonClient(SimpleNamespace(ozon_client_id="id", ozon_api_key="key"))

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"result": {"rating": 0, "score": 5, "product": {"name": "Товар"}}}

        async def fake_post_first_available(*args, **kwargs):
            return FakeResponse()

        client._post_first_available = fake_post_first_available
        review = __import__("asyncio").run(client._load_review_info(None, "ozon-review", {}))

        self.assertEqual(review.rating, 0)
        self.assertEqual(review.rating_source, "rating")
        self.assertEqual(review.rating_raw, "0")

    def test_ozon_fetch_review_by_id_unsupported_endpoint_returns_none(self):
        client = OzonClient(SimpleNamespace(ozon_client_id="id", ozon_api_key="key"))

        class FakeResponse:
            status_code = 404
            text = ""

            def json(self):
                return {"result": {}}

        async def fake_post_first_available(*args, **kwargs):
            return FakeResponse()

        client._post_first_available = fake_post_first_available

        self.assertIsNone(asyncio.run(client.fetch_review_by_id("missing")))

    def test_bad_rating_cannot_select_positive_template(self):
        bad = template("bad", 1, 2, "bad answer")
        positive = template("positive", 4, 5, "positive answer")

        review = Review(id="low", rating=1, kind="text")

        self.assertIs(choose_template([positive], review), None)
        self.assertIs(choose_template([bad, positive], review), bad)

    def test_narrow_bad_template_beats_newer_broad_positive_template(self):
        bad = template_with_created_at("bad", 1, 2, "bad answer", datetime(2026, 1, 1))
        broad_positive = template_with_created_at("positive", 1, 5, "positive answer", datetime(2026, 1, 5))

        selected = choose_template([broad_positive, bad], Review(id="low", rating=1, kind="text"))

        self.assertIs(selected, bad)

    def test_narrow_bad_template_beats_broad_article_positive_template(self):
        bad = template_with_created_at("bad", 1, 2, "bad answer", datetime(2026, 1, 1))
        article_positive = article_template(
            "article-positive",
            1,
            5,
            "positive answer",
            "ART-100",
            datetime(2026, 1, 5),
        )

        selected = choose_template(
            [article_positive, bad],
            Review(id="low", article="ART-100-red", rating=1, kind="text"),
        )

        self.assertIs(selected, bad)

    def test_unknown_rating_selects_no_template(self):
        any_rating = template("any", 1, 5, "generic answer")

        self.assertIs(choose_template([any_rating], Review(id="missing", rating=0, kind="text")), None)
        self.assertIs(choose_template([any_rating], Review(id="invalid", rating=6, kind="text")), None)
        self.assertIs(choose_template([any_rating], Review(id="none", rating=None, kind="text")), None)
        self.assertIs(choose_template([any_rating], Review(id="bad-string", rating="bad", kind="text")), None)
        self.assertIs(choose_template([any_rating], Review(id="string", rating="5", kind="text")), any_rating)

    def test_low_or_neutral_rating_does_not_use_broad_one_to_five_template(self):
        broad = template("broad", 1, 5, "generic positive answer")

        self.assertIsNone(choose_template([broad], Review(id="low", rating=1, kind="text")))
        self.assertIsNone(choose_template([broad], Review(id="neutral", rating=3, kind="text")))
        self.assertIs(choose_template([broad], Review(id="good", rating=4, kind="text")), broad)
        self.assertIs(choose_template([broad], Review(id="high", rating=5, kind="text")), broad)

    def test_randomized_template_selection_never_ignores_rating_range(self):
        rng = random.Random(20260826)

        for index in range(500):
            templates = []
            for template_index in range(rng.randint(1, 8)):
                start = rng.randint(1, 5)
                end = rng.randint(start, 5)
                templates.append(template(f"t-{index}-{template_index}", start, end, f"answer {template_index}"))

            rating = rng.randint(-2, 8)
            selected = choose_template(templates, Review(id=str(index), rating=rating, kind="text"))

            if not 1 <= rating <= 5:
                self.assertIsNone(selected)
            elif selected is not None:
                self.assertLessEqual(selected.rating_from, rating)
                self.assertGreaterEqual(selected.rating_to, rating)
                if rating < 4:
                    self.assertLess(selected.rating_to, 4)
                matching_spans = [
                    item.rating_to - item.rating_from
                    for item in templates
                    if item.rating_from <= rating <= item.rating_to
                    and (rating >= 4 or item.rating_to < 4)
                ]
                self.assertEqual(selected.rating_to - selected.rating_from, min(matching_spans))

    def test_positive_text_blocks_narrow_low_rating_template(self):
        risky_low = template("risky-low", 1, 2, "Спасибо за высокую оценку")

        selected = choose_template([risky_low], Review(id="low", rating=1, kind="text"))

        self.assertIsNone(selected)
        self.assertIn("текст похож на позитивный ответ", template_safety_warning(risky_low))

    def test_warning_positive_markers_are_blocked_for_low_rating(self):
        risky_texts = (
            "Спасибо за отличную оценку",
            "Нам очень приятно получить ваш отзыв",
            "Спасибо за хорошую оценку",
            "Рады, что вам понравился товар",
            "Positive feedback response",
        )

        for index, body in enumerate(risky_texts):
            with self.subTest(body=body):
                risky_low = template(f"risky-low-{index}", 1, 2, body)
                selected = choose_template([risky_low], Review(id=f"low-{index}", rating=1, kind="text"))

                self.assertIsNone(selected)
                self.assertIn("текст похож на позитивный ответ", template_safety_warning(risky_low))

    def test_safe_low_template_beats_positive_low_template(self):
        safe_low = template("safe-low", 1, 2, "Жаль, что товар не оправдал ожидания.")
        risky_low = template("risky-low", 1, 2, "Спасибо за высокую оценку")

        selected = choose_template([risky_low, safe_low], Review(id="low", rating=1, kind="text"))

        self.assertIs(selected, safe_low)

    def test_process_user_dry_run_never_calls_send_reply(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.add(template("bad", 1, 2, "bad answer", user.id))
        db.add(template("positive", 4, 5, "positive answer", user.id))
        db.commit()

        class FakeClient:
            async def fetch_queue_snapshot(self, take):
                return ReviewFetchSnapshot(
                    reviews=[
                        Review(id="low", product_name="Товар", rating=1, kind="text"),
                        Review(id="unknown", product_name="Товар", rating=0, kind="text"),
                    ],
                    diagnostics="fake diagnostics",
                )

            async def send_reply(self, review_id, text):
                raise AssertionError("send_reply must not be called in dry-run")

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "1"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = __import__("asyncio").run(process_user(db, user, manual=True))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous
            db.close()

        db = SessionLocal()
        try:
            messages = [log.message for log in db.query(ReplyLog).order_by(ReplyLog.id).all()]
        finally:
            db.close()

        self.assertEqual(processed, 0)
        self.assertTrue(any("DRY RUN: ответ не отправлен" in message for message in messages))
        self.assertTrue(any("Оценка не определена или вне диапазона 1–5: 0" in message for message in messages))

    def test_build_queue_adds_all_items_before_processing(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.add(template("bad", 1, 2, "bad answer", user.id))
        db.add(template("positive", 4, 5, "positive answer", user.id))
        db.commit()
        db.refresh(cabinet)

        templates = db.query(ReplyTemplate).order_by(ReplyTemplate.id).all()

        class FakeClient:
            async def fetch_queue_snapshot(self, take):
                return ReviewFetchSnapshot(
                    reviews=[
                        Review(id="low", product_name="Товар", rating=1, rating_raw="1", rating_source="test", kind="text"),
                        Review(id="high", product_name="Товар", rating=5, rating_raw="5", rating_source="test", kind="text"),
                        Review(id="unknown", product_name="Товар", rating=0, rating_raw="", rating_source="test", kind="text"),
                    ],
                    diagnostics="fake diagnostics",
                )

        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                asyncio.run(_build_queue(db, user, [cabinet], templates))

            items = db.query(ProcessingQueueItem).order_by(ProcessingQueueItem.position).all()
            logs = db.query(ReplyLog).order_by(ReplyLog.id).all()
        finally:
            db.close()

        self.assertEqual([item.review_id for item in items], ["low", "high", "unknown"])
        self.assertEqual([item.action for item in items], ["send", "send", "skip"])
        self.assertEqual(items[0].template_name, "bad")
        self.assertEqual(items[1].template_name, "positive")
        self.assertEqual(items[2].template_name, "")
        self.assertTrue(all(item.status == "queued" for item in items))
        self.assertTrue(any("Очередь создана: к отправке 2, пропусков 1" in log.message for log in logs))

    def test_process_user_dry_run_preserves_rating_template_mapping(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.add(template("bad", 1, 2, "bad answer {оценка}", user.id))
        db.add(template("positive", 4, 5, "positive answer {оценка}", user.id))
        db.commit()

        class FakeClient:
            async def fetch_queue_snapshot(self, take):
                return ReviewFetchSnapshot(
                    reviews=[
                        Review(id="low", product_name="Товар", rating=1, rating_raw="1", rating_source="test", kind="text"),
                        Review(id="high", product_name="Товар", rating=5, rating_raw="5", rating_source="test", kind="text"),
                        Review(id="unknown", product_name="Товар", rating=0, rating_raw="", rating_source="test", kind="text"),
                    ],
                    diagnostics="fake diagnostics",
                )

            async def send_reply(self, review_id, text):
                raise AssertionError("send_reply must not be called in dry-run")

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "1"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = __import__("asyncio").run(process_user(db, user, manual=True))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        logs = {log.review_id: log for log in db.query(ReplyLog).filter(ReplyLog.review_id != "").all()}
        db.close()

        self.assertEqual(processed, 0)
        self.assertEqual(logs["low"].response_text, "bad answer 1")
        self.assertEqual(logs["low"].rating, 1)
        self.assertEqual(logs["high"].response_text, "positive answer 5")
        self.assertEqual(logs["high"].rating, 5)
        self.assertEqual(logs["unknown"].response_text, "")
        self.assertIn("Оценка не определена", logs["unknown"].message)

    def test_process_user_dry_run_prefers_narrow_bad_over_article_broad_positive(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.add(template("bad", 1, 2, "bad answer {оценка}", user.id))
        db.add(article_template("article-positive", 1, 5, "positive answer {оценка}", "ART-100", datetime(2026, 1, 5), user.id))
        db.commit()

        class FakeClient:
            async def fetch_queue_snapshot(self, take):
                return ReviewFetchSnapshot(
                    reviews=[
                        Review(
                            id="low-article",
                            product_name="Товар",
                            article="ART-100-red",
                            rating=1,
                            rating_raw="1",
                            rating_source="test",
                            kind="text",
                        ),
                    ],
                    diagnostics="fake diagnostics",
                )

            async def send_reply(self, review_id, text):
                raise AssertionError("send_reply must not be called in dry-run")

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "1"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = __import__("asyncio").run(process_user(db, user, manual=True))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        log = db.query(ReplyLog).filter(ReplyLog.review_id == "low-article").one()
        db.close()

        self.assertEqual(processed, 0)
        self.assertEqual(log.response_text, "bad answer 1")
        self.assertEqual(log.rating, 1)
        self.assertIn("DRY RUN", log.message)

    def test_process_user_dry_run_skips_low_or_neutral_rating_when_only_broad_template_exists(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.add(template("broad-positive", 1, 5, "positive answer {оценка}", user.id))
        db.commit()

        class FakeClient:
            async def fetch_queue_snapshot(self, take):
                return ReviewFetchSnapshot(
                    reviews=[
                        Review(id="low-broad", product_name="Товар", rating=1, rating_raw="1", rating_source="test", kind="text"),
                        Review(id="neutral-broad", product_name="Товар", rating=3, rating_raw="3", rating_source="test", kind="text"),
                    ],
                    diagnostics="fake diagnostics",
                )

            async def send_reply(self, review_id, text):
                raise AssertionError("send_reply must not be called in dry-run")

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "1"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = __import__("asyncio").run(process_user(db, user, manual=True))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        log = db.query(ReplyLog).filter(ReplyLog.review_id == "low-broad").one()
        neutral_log = db.query(ReplyLog).filter(ReplyLog.review_id == "neutral-broad").one()
        db.close()

        self.assertEqual(processed, 0)
        self.assertEqual(log.status, "skipped")
        self.assertEqual(log.response_text, "")
        self.assertIn("слишком широкий", log.message)
        self.assertEqual(neutral_log.status, "skipped")
        self.assertEqual(neutral_log.response_text, "")
        self.assertIn("слишком широкий", neutral_log.message)

    def test_unsafe_template_range_logs_configuration_warning(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        broad = template("broad-positive", 1, 5, "positive answer", user.id)
        narrow = template("narrow-bad", 1, 3, "bad answer", user.id)

        log_unsafe_template_range(db, user.id, broad)
        log_unsafe_template_range(db, user.id, narrow)

        logs = db.query(ReplyLog).order_by(ReplyLog.id).all()
        db.close()

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].status, "info")
        self.assertIn("broad-positive", logs[0].message)
        self.assertIn("будет заблокирован", logs[0].message)

    def test_positive_low_rating_template_logs_configuration_warning(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        risky = template("risky-low", 1, 2, "Спасибо за высокую оценку!", user.id)
        safe = template("safe-low", 1, 2, "Жаль, что товар не оправдал ожидания.", user.id)

        log_unsafe_template_range(db, user.id, risky)
        log_unsafe_template_range(db, user.id, safe)

        logs = db.query(ReplyLog).order_by(ReplyLog.id).all()
        db.close()

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].status, "info")
        self.assertIn("risky-low", logs[0].message)
        self.assertIn("текст похож на позитивный ответ", logs[0].message)

    def test_template_safety_warning_helper(self):
        broad = template("broad-positive", 1, 5, "positive answer")
        safe_bad = template("safe-bad", 1, 3, "bad answer")
        safe_good = template("safe-good", 4, 5, "positive answer")
        risky_low = template("risky-low", 1, 2, "Спасибо за высокую оценку!")

        self.assertIn("будет заблокирован", template_safety_warning(broad))
        self.assertEqual(template_safety_warning(safe_bad), "")
        self.assertEqual(template_safety_warning(safe_good), "")
        self.assertIn("текст похож на позитивный ответ", template_safety_warning(risky_low))

    def test_stale_queue_item_with_invalid_rating_is_not_sent(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.commit()
        db.refresh(cabinet)

        db.add(
            ProcessingQueueItem(
                user_id=user.id,
                cabinet_id=cabinet.id,
                cabinet_name=cabinet.name,
                marketplace=cabinet.marketplace,
                review_id="stale",
                product_name="Товар",
                rating=0,
                action="send",
                status="queued",
                response_text="positive answer",
                position=1,
            )
        )
        db.commit()

        class FakeClient:
            async def send_reply(self, review_id, text):
                raise AssertionError("send_reply must not be called for invalid rating")

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "0"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = __import__("asyncio").run(_process_queue(db, user.id))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        messages = [log.message for log in db.query(ReplyLog).order_by(ReplyLog.id).all()]
        remaining = db.query(ProcessingQueueItem).count()
        db.close()

        self.assertEqual(processed, 0)
        self.assertEqual(remaining, 0)
        self.assertTrue(any("Оценка не определена или вне диапазона 1–5: 0" in message for message in messages))

    def test_stale_queue_item_with_string_rating_is_normalized_before_send(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.add(template("positive", 4, 5, "positive answer 5", user.id))
        db.commit()
        db.refresh(cabinet)

        db.add(
            ProcessingQueueItem(
                user_id=user.id,
                cabinet_id=cabinet.id,
                cabinet_name=cabinet.name,
                marketplace=cabinet.marketplace,
                review_id="string-rating",
                product_name="Товар",
                rating="5",
                review_kind="text",
                action="send",
                status="queued",
                template_name="positive",
                response_text="positive answer 5",
                position=1,
            )
        )
        db.commit()
        send_calls = []

        class FakeClient:
            async def send_reply(self, review_id, text):
                send_calls.append((review_id, text))

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "0"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = asyncio.run(_process_queue(db, user.id))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        logs = db.query(ReplyLog).order_by(ReplyLog.id).all()
        remaining = db.query(ProcessingQueueItem).count()
        db.close()

        self.assertEqual(processed, 1)
        self.assertEqual(send_calls, [("string-rating", "positive answer 5")])
        self.assertEqual(remaining, 0)
        self.assertEqual(logs[-1].rating, 5)

    def test_stale_queue_item_with_bad_string_rating_is_skipped_without_crash(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.commit()
        db.refresh(cabinet)

        db.add(
            ProcessingQueueItem(
                user_id=user.id,
                cabinet_id=cabinet.id,
                cabinet_name=cabinet.name,
                marketplace=cabinet.marketplace,
                review_id="bad-string-rating",
                product_name="Товар",
                rating="bad",
                action="send",
                status="queued",
                response_text="positive answer",
                position=1,
            )
        )
        db.commit()

        class FakeClient:
            async def send_reply(self, review_id, text):
                raise AssertionError("send_reply must not be called for bad string rating")

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "0"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = asyncio.run(_process_queue(db, user.id))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        logs = db.query(ReplyLog).order_by(ReplyLog.id).all()
        remaining = db.query(ProcessingQueueItem).count()
        db.close()

        self.assertEqual(processed, 0)
        self.assertEqual(remaining, 0)
        self.assertEqual(logs[-1].status, "skipped")
        self.assertEqual(logs[-1].rating, 0)
        self.assertIn("Оценка не определена", logs[-1].message)

    def test_stale_positive_queue_item_for_bad_rating_is_not_sent(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.add(template("bad", 1, 2, "bad answer", user.id))
        db.add(template("positive", 4, 5, "positive answer", user.id))
        db.commit()
        db.refresh(cabinet)

        db.add(
            ProcessingQueueItem(
                user_id=user.id,
                cabinet_id=cabinet.id,
                cabinet_name=cabinet.name,
                marketplace=cabinet.marketplace,
                review_id="stale-positive",
                product_name="Товар",
                rating=1,
                review_kind="text",
                action="send",
                status="queued",
                template_name="positive",
                response_text="positive answer",
                position=1,
            )
        )
        db.commit()

        class FakeClient:
            async def send_reply(self, review_id, text):
                raise AssertionError("send_reply must not be called for mismatched queue item")

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "0"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = __import__("asyncio").run(_process_queue(db, user.id))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        logs = db.query(ReplyLog).order_by(ReplyLog.id).all()
        remaining = db.query(ProcessingQueueItem).count()
        db.close()

        self.assertEqual(processed, 0)
        self.assertEqual(remaining, 0)
        self.assertEqual(logs[-1].status, "skipped")
        self.assertEqual(logs[-1].rating, 1)
        self.assertIn("шаблон или текст ответа больше не совпадает", logs[-1].message)

    def test_stale_queue_item_with_duplicate_template_name_uses_template_id(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        low = template("duplicate", 1, 2, "same answer", user.id)
        high = template("duplicate", 4, 5, "same answer", user.id)
        db.add(low)
        db.add(high)
        db.commit()
        db.refresh(cabinet)
        db.refresh(low)
        db.refresh(high)

        db.add(
            ProcessingQueueItem(
                user_id=user.id,
                cabinet_id=cabinet.id,
                cabinet_name=cabinet.name,
                marketplace=cabinet.marketplace,
                review_id="duplicate-template",
                product_name="Товар",
                rating=1,
                review_kind="text",
                action="send",
                status="queued",
                template_id=high.id,
                template_name="duplicate",
                response_text="same answer",
                position=1,
            )
        )
        db.commit()

        class FakeClient:
            async def send_reply(self, review_id, text):
                raise AssertionError("send_reply must not be called for template id mismatch")

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "0"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = asyncio.run(_process_queue(db, user.id))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        logs = db.query(ReplyLog).order_by(ReplyLog.id).all()
        remaining = db.query(ProcessingQueueItem).count()
        db.close()

        self.assertEqual(processed, 0)
        self.assertEqual(remaining, 0)
        self.assertEqual(logs[-1].status, "skipped")
        self.assertIn("id шаблона больше не совпадает", logs[-1].message)

    def test_latest_bad_rating_before_send_blocks_positive_queue_item(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.add(template("bad", 1, 2, "bad answer", user.id))
        db.add(template("positive", 4, 5, "positive answer", user.id))
        db.commit()
        db.refresh(cabinet)

        db.add(
            ProcessingQueueItem(
                user_id=user.id,
                cabinet_id=cabinet.id,
                cabinet_name=cabinet.name,
                marketplace=cabinet.marketplace,
                review_id="changed-rating",
                product_name="Товар",
                rating=5,
                review_kind="text",
                action="send",
                status="queued",
                template_name="positive",
                response_text="positive answer",
                position=1,
            )
        )
        db.commit()

        class FakeClient:
            async def fetch_review_by_id(self, review_id):
                return Review(id=review_id, product_name="Товар", rating=1, rating_raw="1", rating_source="latest", kind="text")

            async def send_reply(self, review_id, text):
                raise AssertionError("send_reply must not be called when latest rating changed")

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "0"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = asyncio.run(_process_queue(db, user.id))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        logs = db.query(ReplyLog).order_by(ReplyLog.id).all()
        remaining = db.query(ProcessingQueueItem).count()
        db.close()

        self.assertEqual(processed, 0)
        self.assertEqual(remaining, 0)
        self.assertEqual(logs[-1].status, "skipped")
        self.assertEqual(logs[-1].rating, 1)
        self.assertIn("шаблон или текст ответа больше не совпадает", logs[-1].message)

    def test_latest_invalid_rating_before_send_blocks_queue_item(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.add(template("positive", 4, 5, "positive answer", user.id))
        db.commit()
        db.refresh(cabinet)

        db.add(
            ProcessingQueueItem(
                user_id=user.id,
                cabinet_id=cabinet.id,
                cabinet_name=cabinet.name,
                marketplace=cabinet.marketplace,
                review_id="invalid-latest",
                product_name="Товар",
                rating=5,
                review_kind="text",
                action="send",
                status="queued",
                template_name="positive",
                response_text="positive answer",
                position=1,
            )
        )
        db.commit()

        class FakeClient:
            async def fetch_review_by_id(self, review_id):
                return Review(id=review_id, product_name="Товар", rating=0, rating_raw="bad", rating_source="latest", kind="text")

            async def send_reply(self, review_id, text):
                raise AssertionError("send_reply must not be called when latest rating is invalid")

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "0"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = asyncio.run(_process_queue(db, user.id))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        logs = db.query(ReplyLog).order_by(ReplyLog.id).all()
        remaining = db.query(ProcessingQueueItem).count()
        db.close()

        self.assertEqual(processed, 0)
        self.assertEqual(remaining, 0)
        self.assertEqual(logs[-1].status, "skipped")
        self.assertEqual(logs[-1].rating, 0)
        self.assertIn("Перед отправкой оценка не определена", logs[-1].message)

    def test_latest_matching_rating_before_send_allows_reply(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.add(template("positive", 4, 5, "positive answer 5", user.id))
        db.commit()
        db.refresh(cabinet)

        db.add(
            ProcessingQueueItem(
                user_id=user.id,
                cabinet_id=cabinet.id,
                cabinet_name=cabinet.name,
                marketplace=cabinet.marketplace,
                review_id="same-rating",
                product_name="Товар",
                rating=5,
                review_kind="text",
                action="send",
                status="queued",
                template_name="positive",
                response_text="positive answer 5",
                position=1,
            )
        )
        db.commit()
        send_calls = []

        class FakeClient:
            async def fetch_review_by_id(self, review_id):
                return Review(id=review_id, product_name="Товар", rating=5, rating_raw="5", rating_source="latest", kind="text")

            async def send_reply(self, review_id, text):
                send_calls.append((review_id, text))

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "0"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = asyncio.run(_process_queue(db, user.id))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        logs = db.query(ReplyLog).order_by(ReplyLog.id).all()
        remaining = db.query(ProcessingQueueItem).count()
        db.close()

        self.assertEqual(processed, 1)
        self.assertEqual(send_calls, [("same-rating", "positive answer 5")])
        self.assertEqual(remaining, 0)
        self.assertEqual(logs[-1].status, "sent")
        self.assertEqual(logs[-1].rating, 5)

    def test_missing_latest_review_before_send_falls_back_to_queue_rating(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.add(template("positive", 4, 5, "positive answer 5", user.id))
        db.commit()
        db.refresh(cabinet)

        db.add(
            ProcessingQueueItem(
                user_id=user.id,
                cabinet_id=cabinet.id,
                cabinet_name=cabinet.name,
                marketplace=cabinet.marketplace,
                review_id="missing-latest",
                product_name="Товар",
                rating=5,
                review_kind="text",
                action="send",
                status="queued",
                template_name="positive",
                response_text="positive answer 5",
                position=1,
            )
        )
        db.commit()
        send_calls = []

        class FakeClient:
            async def fetch_review_by_id(self, review_id):
                return None

            async def send_reply(self, review_id, text):
                send_calls.append((review_id, text))

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "0"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = asyncio.run(_process_queue(db, user.id))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        logs = db.query(ReplyLog).order_by(ReplyLog.id).all()
        remaining = db.query(ProcessingQueueItem).count()
        db.close()

        self.assertEqual(processed, 1)
        self.assertEqual(send_calls, [("missing-latest", "positive answer 5")])
        self.assertEqual(remaining, 0)
        self.assertEqual(logs[-1].status, "sent")
        self.assertEqual(logs[-1].rating, 5)

    def test_different_latest_review_id_before_send_blocks_reply(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.add(template("positive", 4, 5, "positive answer 5", user.id))
        db.commit()
        db.refresh(cabinet)

        db.add(
            ProcessingQueueItem(
                user_id=user.id,
                cabinet_id=cabinet.id,
                cabinet_name=cabinet.name,
                marketplace=cabinet.marketplace,
                review_id="original-review",
                product_name="Товар",
                rating=5,
                review_kind="text",
                action="send",
                status="queued",
                template_name="positive",
                response_text="positive answer 5",
                position=1,
            )
        )
        db.commit()

        class FakeClient:
            async def fetch_review_by_id(self, review_id):
                return Review(id="other-review", product_name="Товар", rating=5, rating_raw="5", rating_source="latest", kind="text")

            async def send_reply(self, review_id, text):
                raise AssertionError("send_reply must not be called for a different latest review id")

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "0"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = asyncio.run(_process_queue(db, user.id))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        logs = db.query(ReplyLog).order_by(ReplyLog.id).all()
        remaining = db.query(ProcessingQueueItem).count()
        db.close()

        self.assertEqual(processed, 0)
        self.assertEqual(remaining, 0)
        self.assertEqual(logs[-1].status, "error")
        self.assertEqual(logs[-1].review_id, "original-review")
        self.assertIn("другой id отзыва: other-review", logs[-1].message)

    def test_refresh_error_before_send_blocks_reply(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        db = SessionLocal()
        user = User(username="tester", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)

        cabinet = Cabinet(user_id=user.id, name="WB", marketplace="wb", wb_api_key="test")
        db.add(cabinet)
        db.add(template("positive", 4, 5, "positive answer", user.id))
        db.commit()
        db.refresh(cabinet)

        db.add(
            ProcessingQueueItem(
                user_id=user.id,
                cabinet_id=cabinet.id,
                cabinet_name=cabinet.name,
                marketplace=cabinet.marketplace,
                review_id="refresh-error",
                product_name="Товар",
                rating=5,
                review_kind="text",
                action="send",
                status="queued",
                template_name="positive",
                response_text="positive answer",
                position=1,
            )
        )
        db.commit()

        class FakeClient:
            async def fetch_review_by_id(self, review_id):
                raise MarketplaceError("temporary failure")

            async def send_reply(self, review_id, text):
                raise AssertionError("send_reply must not be called when refresh fails")

        previous = os.environ.get("REPLY_DRY_RUN")
        os.environ["REPLY_DRY_RUN"] = "0"
        try:
            with patch("app.worker.client_for", return_value=FakeClient()):
                processed = asyncio.run(_process_queue(db, user.id))
        finally:
            if previous is None:
                os.environ.pop("REPLY_DRY_RUN", None)
            else:
                os.environ["REPLY_DRY_RUN"] = previous

        logs = db.query(ReplyLog).order_by(ReplyLog.id).all()
        remaining = db.query(ProcessingQueueItem).count()
        db.close()

        self.assertEqual(processed, 0)
        self.assertEqual(remaining, 0)
        self.assertEqual(logs[-1].status, "error")
        self.assertIn("не удалось повторно проверить оценку", logs[-1].message)


if __name__ == "__main__":
    unittest.main()
