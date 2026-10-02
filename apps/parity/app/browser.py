"""Server-owned queue for an explicitly started collection in the user's browser.

All calls arrive through the authenticated same-origin Parity tab. No marketplace
cookies, account names, arbitrary URLs or HTML are accepted by this API.
"""
import hmac
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import case, delete, func, select
from .domain import PRICE_TYPES, now
from .models import ActiveJob, BrowserRun, BrowserTask, CollectionAttempt, Policy, Product, Snapshot
from .service import enqueue, log, recalculate, summarize_errors
from .storefront import CollectionError, ERRORS, normalize_observation, product_url

ATTENTION = {**{code: message for code, message in ERRORS.items() if code != "region"},
             "cancelled": "Сбор остановлен пользователем. Уже полученные снимки сохранены."}


def start(session, owner, marketplace=None, target=None, probe=False):
    active = session.get(ActiveJob, owner)
    if active:
        from .models import Job
        return session.get(Job, active.job_id)
    query = select(Product).where(Product.owner_id == owner, Product.present.is_(True)).order_by(Product.marketplace, Product.id)
    if marketplace:
        query = query.where(Product.marketplace == marketplace)
    if target:
        query = query.where(Product.id == target)
    if probe:
        query = query.limit(1)
    products = list(session.scalars(query))
    if not products:
        raise HTTPException(422, ERRORS["no_products"])
    job = enqueue(session, owner, marketplace, "browser", target_product_id=target)
    job.phase = "waiting_browser"
    session.add(BrowserRun(job_id=job.id))
    for product in products:
        session.add(BrowserTask(job_id=job.id, product_id=product.id, token=uuid4().hex))
        product.collection_status, product.collection_error = "pending", None
    recalculate(session, owner)
    session.flush()
    return job


def view(session, job):
    run = session.get(BrowserRun, job.id)
    if not run:
        raise HTTPException(404, "Задание расширения не найдено.")
    total, done = session.execute(select(func.count(), func.sum(case((BrowserTask.state != "pending", 1), else_=0))).where(BrowserTask.job_id == job.id)).one()
    current = session.scalar(select(BrowserTask).where(BrowserTask.job_id == job.id, BrowserTask.state == "pending").order_by(BrowserTask.id).limit(1))
    product = session.get(Product, current.product_id) if current else None
    return {"state": run.state, "total": total, "done": done or 0, "attention_code": run.attention_code,
            "saved": job.prices_updated, "skipped": job.errors_count,
            "message": ATTENTION.get(run.attention_code, ""),
            "current": {"article": product.seller_article, "marketplace": product.marketplace} if product else None}


def finish(session, job, run, cancelled=False):
    tasks = list(session.scalars(select(BrowserTask).where(BrowserTask.job_id == job.id)))
    if cancelled:
        for task in tasks:
            if task.state == "pending":
                task.state, task.error_code = "skipped", "cancelled"
                product = session.get(Product, task.product_id)
                product.collection_status, product.collection_error = "failed", "cancelled"
                job.errors_count += 1
                session.add(CollectionAttempt(owner_id=job.owner_id, job_id=job.id, product_id=product.id, status="skipped", error_code="cancelled"))
    results = {}
    for task in tasks:
        product = session.get(Product, task.product_id)
        result = results.setdefault(product.marketplace, {"successes": 0, "errors": 0, "attempted": 0, "skipped": 0})
        result["successes" if task.state == "success" else "errors"] += 1
        result["skipped" if task.state == "skipped" else "attempted"] += 1
    for result in results.values():
        result["status"] = "success" if not result["errors"] else "partial" if result["successes"] else "failed"
        result["message"] = ATTENTION["cancelled"] if cancelled else "Некоторые карточки пропущены. Причины доступны в журнале." if result["errors"] else None
    job.results, job.error_summary = results, summarize_errors(results)
    job.status = "partial" if job.prices_updated and job.errors_count else "success" if not job.errors_count else "failed"
    if cancelled:
        job.status = "cancelled"
    job.phase, job.finished_at = "complete", now()
    run.state, run.attention_code = "finished", None
    session.execute(delete(ActiveJob).where(ActiveJob.job_id == job.id))
    recalculate(session, job.owner_id, job.id)
    log(session, job.owner_id, "sync_finish", job.id, status=job.status)


def skip_task(session, job, task, code, automatic=False):
    """Record a failed card once, without accepting its price or scanning the catalogue."""
    if task.state != "pending":
        return
    task.state, task.error_code = "skipped", code
    product = session.get(Product, task.product_id)
    product.collection_status, product.collection_error = "failed", code
    job.errors_count += 1
    session.add(CollectionAttempt(owner_id=job.owner_id, job_id=job.id, product_id=product.id,
                                  status="skipped", error_code=code, details={"automatic": automatic}))
    log(session, job.owner_id, "collection_error", job.id, product.id,
        code=code, skipped=True, automatic=automatic)
    session.flush()


def claim(session, job, client_id, timeout_seconds=30):
    run = session.get(BrowserRun, job.id)
    if not run:
        raise HTTPException(404, "Задание расширения не найдено.")
    if job.finished_at:
        # An owner may inspect the terminal state after reinstalling/updating the
        # extension. It returns no task and must not trap a stale local cursor.
        return {**view(session, job), "task": None}
    if run.client_id and run.client_id != client_id:
        raise HTTPException(409, "Задание связано с другим профилем браузера. Продолжите в нём или остановите задание.")
    policy = session.get(Policy, job.owner_id)
    if not run.client_id:
        run.client_id = client_id
        if policy.browser_client_id != client_id or policy.price_source != "storefront":
            policy.context_version += 1
        policy.price_source, policy.browser_client_id, policy.enabled = "storefront", client_id, False
        run.context_version = policy.context_version
        if run.state == "waiting_browser":
            run.state = "running"
        job.started_at = now()
        log(session, job.owner_id, "sync_start", job.id, collector="extension")
        recalculate(session, job.owner_id)
    # Recover an existing non-blocking stop on the next authenticated heartbeat of
    # its bound browser. Only the user's explicit pause keeps a live run paused.
    if run.state == "attention":
        task = session.scalar(select(BrowserTask).where(BrowserTask.job_id == job.id, BrowserTask.state == "pending").order_by(BrowserTask.id).limit(1))
        if task:
            skip_task(session, job, task, "unavailable" if run.attention_code in {"setup", "region"} else run.attention_code or "internal", automatic=True)
        run.state, run.attention_code = "running", None
    job.heartbeat_at, job.phase = now(), run.state
    if run.state != "running":
        return {**view(session, job), "task": None}
    while True:
        task = session.scalar(select(BrowserTask).where(BrowserTask.job_id == job.id, BrowserTask.state == "pending").order_by(BrowserTask.id).limit(1))
        if not task:
            finish(session, job, run)
            return {**view(session, job), "task": None}
        product = session.get(Product, task.product_id)
        try:
            url = product_url(product.marketplace, product.external_id, product.sku)
        except CollectionError as exc:
            skip_task(session, job, task, exc.code, automatic=True)
            continue
        break
    return {**view(session, job), "task": {"id": task.id, "token": task.token, "url": url,
            "marketplace": product.marketplace, "identifier": product.external_id if product.marketplace == "wb" else product.sku,
            "interval_seconds": policy.page_interval_seconds, "timeout_seconds": timeout_seconds}}


def receipt(session, job, client_id, task_id, token, data=None, error_code=None):
    run = session.get(BrowserRun, job.id)
    task = session.get(BrowserTask, task_id)
    if not run or run.client_id != client_id or not task or task.job_id != job.id or not hmac.compare_digest(task.token, token):
        raise HTTPException(409, "Наблюдение не соответствует текущему заданию браузера.")
    if task.state != "pending":
        return {"accepted": True, "duplicate": True}
    if job.finished_at or run.state != "running":
        raise HTTPException(409, "Сбор приостановлен или завершён. Наблюдение не сохранено.")
    current = session.scalar(select(BrowserTask.id).where(BrowserTask.job_id == job.id, BrowserTask.state == "pending").order_by(BrowserTask.id).limit(1))
    if current != task.id:
        raise HTTPException(409, "Карточка ещё не назначена для сбора.")
    product = session.get(Product, task.product_id)
    policy = session.get(Policy, job.owner_id)
    observation = None
    code = error_code
    if not code:
        try:
            url = product_url(product.marketplace, product.external_id, product.sku)
            observation = normalize_observation(data, product.marketplace, product.external_id if product.marketplace == "wb" else product.sku, policy.city, url, check_city=False)
        except CollectionError as exc:
            code = exc.code
    if code:
        skip_task(session, job, task, "unavailable" if code in {"setup", "region"} else code, automatic=True)
        if not session.scalar(select(BrowserTask.id).where(BrowserTask.job_id == job.id, BrowserTask.state == "pending").limit(1)):
            finish(session, job, run)
        return {"accepted": True, **view(session, job)}
    observation.context.update(session="browser_profile", collector="extension")
    snapshot = Snapshot(product_id=product.id, job_id=job.id, source_kind="storefront", context_version=run.context_version,
                        context=observation.context, page_url=observation.page_url,
                        **{key: getattr(observation.price, key) for key in PRICE_TYPES},
                        currency=observation.price.currency, sources=observation.price.sources, reasons=observation.price.reasons)
    session.add(snapshot)
    session.flush()
    product.current_snapshot_id, product.updated_at = snapshot.id, snapshot.captured_at
    product.price_source, product.price_context_version = "storefront", run.context_version
    product.collection_status, product.collection_error = "success", None
    task.state = "success"
    job.products_received += 1
    job.products_updated += 1
    job.prices_updated += 1
    session.add(CollectionAttempt(owner_id=job.owner_id, job_id=job.id, product_id=product.id, status="success", details={"collector": "extension", "context_version": run.context_version}))
    log(session, job.owner_id, "price_snapshot_created", job.id, product.id, source="storefront", collector="extension", city=policy.city)
    session.flush()
    if not session.scalar(select(BrowserTask.id).where(BrowserTask.job_id == job.id, BrowserTask.state == "pending").limit(1)):
        finish(session, job, run)
    # Comparisons stay stale during collection and are recalculated on finish
    # (including explicit stop). Do not scan the entire catalogue per card.
    return {"accepted": True, **view(session, job)}


def control(session, job, action):
    run = session.get(BrowserRun, job.id)
    if not run:
        raise HTTPException(404, "Задание расширения не найдено.")
    if job.finished_at:
        return
    if action == "stop":
        finish(session, job, run, cancelled=True)
    elif action == "pause":
        run.state, job.phase = "paused", "paused"
    elif action == "resume":
        run.state, run.attention_code = ("running" if run.client_id else "waiting_browser"), None
        job.phase = run.state
    elif action == "skip":
        task = session.scalar(select(BrowserTask).where(BrowserTask.job_id == job.id, BrowserTask.state == "pending").order_by(BrowserTask.id).limit(1))
        if task:
            skip_task(session, job, task, run.attention_code or "layout")
        run.state, run.attention_code = ("running" if run.client_id else "waiting_browser"), None
        job.phase = run.state
        session.flush()
        if not session.scalar(select(BrowserTask.id).where(BrowserTask.job_id == job.id, BrowserTask.state == "pending").limit(1)):
            finish(session, job, run)
    else:
        raise HTTPException(422, "Неизвестное действие.")
