import os
import re
import threading
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .marketplaces import MarketplaceError, Review, WB_FEEDBACK_TAKE_LIMIT, WB_QUESTIONS_REVIEWS_INTERVAL_SECONDS, client_for, parse_rating
from .models import Cabinet, ProcessingQueueItem, ReplyLog, ReplyTemplate, ScheduleSetting, User


PRODUCT_CATEGORIES = ("Футболки", "Обои", "Фотосетка")
POSITIVE_LOW_RATING_MARKERS = (
    "высокую оцен",
    "высокая оцен",
    "отличн",
    "прекрасн",
    "замечательн",
    "рады что вам понрав",
    "рады, что вам понрав",
    "очень приятно",
    "положительн",
    "5 звезд",
    "5-звезд",
    "пять звезд",
    "хорошую оцен",
    "positive",
)
PRODUCT_CATEGORY_RULES: dict[str, tuple[tuple[str, ...], ...]] = {
    "Футболки": (("футболк",),),
    "Обои": (("фотообо",), ("обои",), ("обоев",),),
    "Фотосетка": (
        ("фотосетк",),
        ("фотофасад",),
        ("фото", "фасад"),
        ("сетка", "забор"),
        ("сетка", "рисунк"),
        ("сетка", "сад"),
        ("сетка", "дач"),
    ),
}
_user_run_locks: dict[int, threading.Lock] = {}
_user_run_locks_guard = threading.Lock()


def get_or_create_settings(db: Session, user_id: int) -> ScheduleSetting:
    settings = db.scalar(select(ScheduleSetting).where(ScheduleSetting.user_id == user_id))
    if settings:
        return settings
    settings = ScheduleSetting(user_id=user_id)
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def due_for_run(settings: ScheduleSetting, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    if not settings.enabled or _in_quiet_hours(settings, now):
        return False
    if settings.last_run_at is None:
        return True
    return now - settings.last_run_at >= timedelta(minutes=settings.interval_minutes)


async def process_user(db: Session, user: User, manual: bool = False) -> int:
    if not _try_acquire_user_run(user.id):
        if manual:
            _add_log(db, user.id, None, "system", "", "skipped", "Обработка уже запущена")
        return 0

    try:
        settings = get_or_create_settings(db, user.id)
        if not manual and not due_for_run(settings):
            return 0

        cabinets = db.scalars(
            select(Cabinet).where(Cabinet.user_id == user.id, Cabinet.is_active.is_(True))
        ).all()
        templates = db.scalars(
            select(ReplyTemplate).where(ReplyTemplate.user_id == user.id, ReplyTemplate.is_active.is_(True))
        ).all()
        processed = 0
        if not cabinets:
            _add_log(db, user.id, None, "system", "", "skipped", "Добавьте хотя бы один кабинет")
        elif not templates:
            _add_log(db, user.id, None, "system", "", "skipped", "Добавьте хотя бы один шаблон")
        else:
            _clear_queue(db, user.id)
            await _build_queue(db, user, cabinets, templates)
            processed = await _process_queue(db, user.id)

        settings.last_run_at = datetime.utcnow()
        db.add(settings)
        db.commit()
        return processed
    finally:
        _release_user_run(user.id)


async def process_due_users(db: Session) -> None:
    users = db.scalars(select(User)).all()
    for user in users:
        await process_user(db, user, manual=False)


async def preview_user_queue(db: Session, user: User) -> list[ProcessingQueueItem]:
    return db.scalars(
        select(ProcessingQueueItem)
        .where(ProcessingQueueItem.user_id == user.id)
        .order_by(ProcessingQueueItem.position.asc(), ProcessingQueueItem.created_at.asc())
    ).all()


def _clear_queue(db: Session, user_id: int) -> None:
    items = db.scalars(select(ProcessingQueueItem).where(ProcessingQueueItem.user_id == user_id)).all()
    for item in items:
        db.delete(item)
    db.commit()


async def _build_queue(db: Session, user: User, cabinets: list[Cabinet], templates: list[ReplyTemplate]) -> None:
    position = 0
    send_position = 0
    created_at = datetime.utcnow()

    for cabinet in cabinets:
        try:
            snapshot = await _fetch_queue_snapshot(cabinet)
        except MarketplaceError as exc:
            _add_log(db, user.id, cabinet.id, cabinet.marketplace, "", "error", str(exc))
            continue

        if snapshot.diagnostics:
            _add_log(db, user.id, cabinet.id, cabinet.marketplace, "", "info", snapshot.diagnostics)

        for review in snapshot.reviews:
            review = _review_with_rating(review, _safe_rating(review.rating))
            position += 1
            rating_valid = _valid_rating(review.rating)
            template = choose_template(templates, review) if rating_valid else None
            detected_categories = ", ".join(detect_product_categories(review))
            response_text = render_answer(template.body, review, cabinet) if template else ""

            if _already_logged(db, user.id, cabinet.id, review.id):
                action = "skip"
                status = "queued"
                message = "Уже отправлен ранее"
                expected_at = created_at
            elif not rating_valid:
                action = "skip"
                status = "queued"
                message = f"Оценка не определена или вне диапазона 1–5: {review.rating}"
                expected_at = created_at
            elif not template:
                action = "skip"
                status = "queued"
                message = _no_template_message(templates, review)
                expected_at = created_at
            else:
                send_position += 1
                action = "send"
                status = "queued"
                delay = max(1, round(send_position * WB_QUESTIONS_REVIEWS_INTERVAL_SECONDS))
                message = f"Будет отправлен примерно через {delay} сек."
                expected_at = created_at + timedelta(seconds=delay)

            db.add(
                ProcessingQueueItem(
                    user_id=user.id,
                    cabinet_id=cabinet.id,
                    cabinet_name=cabinet.name,
                    marketplace=cabinet.marketplace,
                    review_id=review.id,
                    product_name=review.product_name,
                    article=review.article,
                    category=review.category,
                    detected_categories=detected_categories,
                    review_kind=review.kind,
                    rating=review.rating,
                    rating_raw=review.rating_raw,
                    rating_source=review.rating_source,
                    template_id=template.id if template else None,
                    template_name=template.name if template else "",
                    response_text=response_text,
                    action=action,
                    status=status,
                    message=message,
                    position=position,
                    expected_at=expected_at,
                )
            )

    db.commit()
    queued = db.scalars(select(ProcessingQueueItem).where(ProcessingQueueItem.user_id == user.id)).all()
    to_send = sum(1 for item in queued if item.action == "send")
    to_skip = sum(1 for item in queued if item.action == "skip")
    _add_log(db, user.id, None, "system", "", "info", f"Очередь создана: к отправке {to_send}, пропусков {to_skip}")


async def _fetch_queue_snapshot(cabinet: Cabinet):
    client = client_for(cabinet)
    if cabinet.marketplace == "wb" and hasattr(client, "fetch_queue_snapshot"):
        return await client.fetch_queue_snapshot(WB_FEEDBACK_TAKE_LIMIT)
    reviews = await client.fetch_reviews_to_answer(WB_FEEDBACK_TAKE_LIMIT)
    return type("QueueSnapshot", (), {"reviews": reviews, "diagnostics": ""})()


async def _process_queue(db: Session, user_id: int) -> int:
    processed = 0

    while True:
        item = db.scalar(
            select(ProcessingQueueItem)
            .where(ProcessingQueueItem.user_id == user_id)
            .order_by(ProcessingQueueItem.position.asc(), ProcessingQueueItem.created_at.asc())
            .limit(1)
        )
        if not item:
            return processed

        cabinet = db.get(Cabinet, item.cabinet_id) if item.cabinet_id else None
        if not cabinet:
            db.delete(item)
            db.commit()
            continue

        review = _review_from_queue_item(item)
        if item.action == "skip":
            _add_review_log(db, user_id, cabinet, review, "", "skipped", item.message)
            db.delete(item)
            db.commit()
            continue

        if not _valid_rating(item.rating):
            _add_review_log(
                db,
                user_id,
                cabinet,
                review,
                item.response_text,
                "skipped",
                f"Оценка не определена или вне диапазона 1–5: {item.rating}",
            )
            db.delete(item)
            db.commit()
            continue

        client = client_for(cabinet)
        review, message = await _refresh_review_before_send(client, review)
        if message:
            _add_review_log(db, user_id, cabinet, review, item.response_text, "error", message)
            db.delete(item)
            db.commit()
            continue

        if not _valid_rating(review.rating):
            _add_review_log(
                db,
                user_id,
                cabinet,
                review,
                item.response_text,
                "skipped",
                f"Перед отправкой оценка не определена или вне диапазона 1–5: {review.rating}",
            )
            db.delete(item)
            db.commit()
            continue

        is_valid, message = _validate_queue_send_item(db, user_id, cabinet, review, item)
        if not is_valid:
            _add_review_log(db, user_id, cabinet, review, item.response_text, "skipped", message)
            db.delete(item)
            db.commit()
            continue

        item.status = "processing"
        item.message = "Отправляется"
        item.updated_at = datetime.utcnow()
        db.add(item)
        db.commit()

        if _dry_run_enabled():
            _add_review_log(db, user_id, cabinet, review, item.response_text, "skipped", "DRY RUN: ответ не отправлен")
        else:
            try:
                await client.send_reply(item.review_id, item.response_text)
            except MarketplaceError as exc:
                _add_review_log(db, user_id, cabinet, review, item.response_text, "error", str(exc))
            else:
                _add_review_log(db, user_id, cabinet, review, item.response_text, "sent", "Ответ отправлен")
                processed += 1

        db.delete(item)
        db.commit()


def _review_from_queue_item(item: ProcessingQueueItem) -> Review:
    return Review(
        id=item.review_id,
        product_name=item.product_name,
        article=item.article,
        category=item.category,
        kind=item.review_kind,
        rating=_safe_rating(item.rating),
        rating_raw=item.rating_raw,
        rating_source=item.rating_source,
    )


def _validate_queue_send_item(
    db: Session,
    user_id: int,
    cabinet: Cabinet,
    review: Review,
    item: ProcessingQueueItem,
) -> tuple[bool, str]:
    templates = db.scalars(
        select(ReplyTemplate).where(ReplyTemplate.user_id == user_id, ReplyTemplate.is_active.is_(True))
    ).all()
    template = choose_template(templates, review)
    if not template:
        return False, f"Перед отправкой {_no_template_message(templates, review).lower()}"

    expected_answer = render_answer(template.body, review, cabinet)
    if item.template_id and template.id != item.template_id:
        return False, "Перед отправкой id шаблона больше не совпадает с оценкой отзыва"
    if item.template_name != template.name or item.response_text != expected_answer:
        return False, "Перед отправкой шаблон или текст ответа больше не совпадает с оценкой отзыва"

    return True, ""


async def _refresh_review_before_send(client, review: Review) -> tuple[Review, str]:
    fetch_review_by_id = getattr(client, "fetch_review_by_id", None)
    if not callable(fetch_review_by_id):
        return review, ""

    try:
        latest = await fetch_review_by_id(review.id)
    except MarketplaceError as exc:
        return review, f"Перед отправкой не удалось повторно проверить оценку: {exc}"

    if latest is None:
        return review, ""
    if latest.id and latest.id != review.id:
        return review, f"Перед отправкой маркетплейс вернул другой id отзыва: {latest.id}"

    return _merge_latest_review(review, latest), ""


def _merge_latest_review(previous: Review, latest: Review) -> Review:
    return Review(
        id=previous.id,
        product_name=latest.product_name or previous.product_name,
        article=latest.article or previous.article,
        category=latest.category or previous.category,
        kind=latest.kind or previous.kind,
        rating=_safe_rating(latest.rating),
        rating_raw=latest.rating_raw,
        rating_source=latest.rating_source or previous.rating_source,
        text=latest.text or previous.text,
        raw=latest.raw or previous.raw,
        info_loaded=latest.info_loaded or previous.info_loaded,
    )


def _try_acquire_user_run(user_id: int) -> bool:
    with _user_run_locks_guard:
        lock = _user_run_locks.setdefault(user_id, threading.Lock())
    return lock.acquire(blocking=False)


def _release_user_run(user_id: int) -> None:
    with _user_run_locks_guard:
        lock = _user_run_locks.get(user_id)
    if lock and lock.locked():
        lock.release()


def choose_template(templates: list[ReplyTemplate], review: Review) -> ReplyTemplate | None:
    rating = _safe_rating(review.rating)
    if not _valid_rating(rating):
        return None

    scored: list[tuple[int, ReplyTemplate]] = []
    product_categories = detect_product_categories(review)
    for template in templates:
        if template.review_kind != "any" and template.review_kind != review.kind:
            continue
        if not (template.rating_from <= rating <= template.rating_to):
            continue
        if not _rating_template_safe(rating, template):
            continue
        if not _template_text_safe_for_rating(rating, template):
            continue

        score = _template_match_score(template, review, product_categories)

        if score:
            scored.append((score, template))
    if not scored:
        return None
    return max(scored, key=_template_priority)[1]


def _template_priority(item: tuple[int, ReplyTemplate]) -> tuple[int, int, datetime]:
    score, template = item
    rating_span = template.rating_to - template.rating_from
    return (-rating_span, score, template.created_at)


def _valid_rating(rating: int) -> bool:
    return _safe_rating(rating) != 0


def _safe_rating(value: object) -> int:
    return parse_rating(value)


def _review_with_rating(review: Review, rating: int) -> Review:
    return Review(
        id=review.id,
        product_name=review.product_name,
        article=review.article,
        category=review.category,
        kind=review.kind,
        rating=rating,
        rating_raw=review.rating_raw,
        rating_source=review.rating_source,
        text=review.text,
        raw=review.raw,
        info_loaded=review.info_loaded,
    )


def _rating_template_safe(rating: int, template: ReplyTemplate) -> bool:
    if rating < 4:
        return template.rating_to < 4
    return True


def _template_text_safe_for_rating(rating: int, template: ReplyTemplate) -> bool:
    if rating >= 4:
        return True
    return not _looks_like_high_rating_answer(template.body)


def _looks_like_high_rating_answer(text: str) -> bool:
    normalized = text.lower().replace("ё", "е")
    return any(marker in normalized for marker in POSITIVE_LOW_RATING_MARKERS)


def _no_template_message(templates: list[ReplyTemplate], review: Review) -> str:
    if _has_unsafe_rating_template(templates, review):
        return "Подходящий шаблон слишком широкий для оценки ниже 4"
    if _has_positive_text_template_for_low_rating(templates, review):
        return "Подходящий шаблон похож на позитивный ответ для оценки ниже 4"
    return "Подходящий шаблон не найден"


def _has_unsafe_rating_template(templates: list[ReplyTemplate], review: Review) -> bool:
    rating = _safe_rating(review.rating)
    if not _valid_rating(rating):
        return False

    product_categories = detect_product_categories(review)
    for template in templates:
        if template.review_kind != "any" and template.review_kind != review.kind:
            continue
        if not (template.rating_from <= rating <= template.rating_to):
            continue
        if _rating_template_safe(rating, template):
            continue
        if _template_match_score(template, review, product_categories):
            return True
    return False


def _has_positive_text_template_for_low_rating(templates: list[ReplyTemplate], review: Review) -> bool:
    rating = _safe_rating(review.rating)
    if not _valid_rating(rating) or rating >= 4:
        return False

    product_categories = detect_product_categories(review)
    for template in templates:
        if template.review_kind != "any" and template.review_kind != review.kind:
            continue
        if not (template.rating_from <= rating <= template.rating_to):
            continue
        if not _rating_template_safe(rating, template):
            continue
        if _template_text_safe_for_rating(rating, template):
            continue
        if _template_match_score(template, review, product_categories):
            return True
    return False


def _template_match_score(template: ReplyTemplate, review: Review, product_categories: list[str]) -> int:
    value = (template.match_value or "").strip().lower()
    if template.match_mode == "all":
        return 10
    if template.match_mode == "category" and _matches_product_category(value, product_categories, review):
        return 20
    if template.match_mode == "article" and value and value in review.article.lower():
        return 30
    return 0


def _dry_run_enabled() -> bool:
    return os.getenv("REPLY_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}


def detect_product_categories(review: Review) -> list[str]:
    text = _normalize_product_text(" ".join(filter(None, [review.product_name, review.category])))
    if not text:
        return []
    return [
        category
        for category, rules in PRODUCT_CATEGORY_RULES.items()
        if any(all(token in text for token in rule) for rule in rules)
    ]


def _matches_product_category(value: str, product_categories: list[str], review: Review) -> bool:
    value = _normalize_product_text(value)
    if not value:
        return False

    if any(value == _normalize_product_text(category) for category in product_categories):
        return True

    source_text = _normalize_product_text(" ".join(filter(None, [review.product_name, review.category])))
    return value in source_text


def _normalize_product_text(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def render_answer(template_text: str, review: Review, cabinet: Cabinet) -> str:
    values = {
        "товар": review.product_name,
        "артикул": review.article,
        "категория": review.category,
        "оценка": str(review.rating or ""),
        "маркетплейс": "Wildberries" if cabinet.marketplace == "wb" else "Ozon",
        "кабинет": cabinet.name,
    }
    answer = template_text
    for key, value in values.items():
        answer = answer.replace("{" + key + "}", value)
    return answer.strip()


def _already_logged(db: Session, user_id: int, cabinet_id: int, review_id: str) -> bool:
    return bool(
        db.scalar(
            select(ReplyLog.id).where(
                ReplyLog.user_id == user_id,
                ReplyLog.cabinet_id == cabinet_id,
                ReplyLog.review_id == review_id,
                ReplyLog.status == "sent",
            )
        )
    )


def _add_review_log(
    db: Session,
    user_id: int,
    cabinet: Cabinet,
    review: Review,
    response_text: str,
    status: str,
    message: str,
) -> None:
    message = _with_rating_diagnostics(message, review)
    log = ReplyLog(
        user_id=user_id,
        cabinet_id=cabinet.id,
        marketplace=cabinet.marketplace,
        review_id=review.id,
        product_name=review.product_name,
        article=review.article,
        category=review.category,
        review_kind=review.kind,
        rating=review.rating,
        response_text=response_text,
        status=status,
        message=message,
    )
    db.add(log)
    db.commit()


def _with_rating_diagnostics(message: str, review: Review) -> str:
    details = [f"rating={review.rating}"]
    if review.rating_source:
        details.append(f"source={review.rating_source}")
    if review.rating_raw:
        details.append(f"raw={review.rating_raw}")
    return f"{message} ({', '.join(details)})"


def _add_log(db: Session, user_id: int, cabinet_id: int | None, marketplace: str, review_id: str, status: str, message: str) -> None:
    log = ReplyLog(
        user_id=user_id,
        cabinet_id=cabinet_id,
        marketplace=marketplace,
        review_id=review_id,
        status=status,
        message=message,
    )
    db.add(log)
    db.commit()


def _in_quiet_hours(settings: ScheduleSetting, now: datetime) -> bool:
    if not settings.quiet_start or not settings.quiet_end:
        return False
    try:
        start = time.fromisoformat(settings.quiet_start)
        end = time.fromisoformat(settings.quiet_end)
    except ValueError:
        return False
    current = now.time().replace(second=0, microsecond=0)
    if start < end:
        return start <= current < end
    return current >= start or current < end
