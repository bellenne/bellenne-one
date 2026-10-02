from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hmac
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from uuid import UUID
from sqlalchemy import func, or_, select
from .adapters import ERRORS
from .config import Settings
from .db import database, migrate
from .domain import PRICE_LABELS, PRICE_TYPES, now
from .models import Account, ActiveJob, BrowserRun, Event, Job, Mapping, Policy, Product, Snapshot
from .queries import catalog, history
from .security import account_view, cipher
from .storefront import ERRORS as COLLECTION_ERRORS, CollectionError, product_url
from .service import enqueue, ensure_owner, is_stale, log, manual_link, recalculate, summarize_errors, unlink, write_lock
from . import browser

ROOT = Path(__file__).parent
STATUS_LABELS = {"matched": "Совпадают", "mismatch": "Расхождение", "missing_wb": "Нет товара WB", "missing_ozon": "Нет товара Ozon", "mapping_required": "Нужна связь", "price_unavailable": "Цены недоступны", "stale": "Данные устарели", "unavailable": "Недоступно", "not_configured": "Не настроено", "not_checked": "Не проверено", "connected": "Подключено", "failed": "Ошибка", "running": "В работе", "success": "Успешно", "partial": "Частично", "not_compared": "Не сравнивается", "pending": "Ожидает сбора", "not_collected": "Ещё не собрана", "cancelled": "Остановлено", "paused": "Пауза", "attention": "Нужно ваше действие"}
REASON_LABELS = {"not_visible": "Не показана на странице", "not_provided": "API не предоставил цену", "size_prices_differ": "Цены размеров отличаются; единой цены нет", "not_supported": "API не предоставляет цену лояльности"}


class BrowserClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csrf_token: str = Field(max_length=500)
    client_id: UUID


class BrowserControl(BrowserClaim):
    action: Literal["pause", "resume", "skip"]


class DomObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    blocked: bool = False
    unavailable: bool = False
    city: str | None = Field(default=None, max_length=80)
    identity: str | None = Field(default=None, max_length=80)
    benefit: str | None = Field(default=None, max_length=80)
    regular: str | None = Field(default=None, max_length=80)
    reference: str | None = Field(default=None, max_length=80)
    benefitConfirmed: bool = False
    hasBox: bool = False


class BrowserReceipt(BrowserClaim):
    task_id: int = Field(gt=0)
    token: str = Field(min_length=32, max_length=64)
    observation: DomObservation | None = None
    error_code: Literal["blocked", "region", "identity", "benefit_missing", "layout", "navigation", "unavailable", "setup", "internal"] | None = None
    observed_at: datetime | None = None


def pagination_query(request, page):
    params = [(key, value) for key, value in request.query_params.multi_items() if key != "page"]
    return "?" + urlencode([*params, ("page", page)])


def format_money(amount, currency="RUB"):
    if amount is None:
        return "Недоступно"
    currency = currency or ""
    value = f"{amount:,.2f}".rstrip("0").rstrip(".").replace(",", " ")
    return f"{value} {'₽' if currency == 'RUB' else currency}"


def create_app(settings=None):
    settings = settings or Settings.from_env()
    engine, factory = database(settings)

    @asynccontextmanager
    async def lifespan(_):
        migrate(engine)
        cipher(settings)
        yield
        engine.dispose()

    app = FastAPI(title="BellenneParity", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.factory, app.state.settings = factory, settings
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")
    templates.env.filters["money"] = format_money
    templates.env.filters["localdate"] = lambda value, zone="Europe/Moscow": value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone)).strftime("%d.%m.%Y %H:%M") if value else "—"
    templates.env.filters["percent"] = lambda value: f"{value:+.1f}%" if value is not None else "Недоступно"

    def db():
        with factory() as session:
            yield session

    def identity(request: Request):
        try:
            user = int(request.headers.get("X-Bellenne-User-Id", ""))
            if user <= 0:
                raise ValueError()
        except ValueError:
            raise HTTPException(401, "Войдите в BellenneOne.") from None
        return user

    def csrf(request, submitted):
        expected = unquote(request.headers.get("X-Bellenne-Csrf-Token", ""))
        if not expected or not submitted or not hmac.compare_digest(expected, submitted):
            raise HTTPException(403, "Сессия формы устарела. Обновите страницу и повторите действие.")

    def account(session, owner, marketplace):
        if marketplace not in ("wb", "ozon"):
            raise HTTPException(404, "Маркетплейс не найден.")
        result = session.scalar(select(Account).where(Account.owner_id == owner, Account.marketplace == marketplace))
        if not result:
            raise HTTPException(404, "Подключение не найдено.")
        return result

    def require_idle(session, owner):
        if session.get(ActiveJob, owner):
            raise HTTPException(409, "Дождитесь завершения текущего задания перед изменением настроек или связей.")

    def render(request, session, active, title, **context):
        owner = identity(request)
        policy = session.get(Policy, owner)
        return templates.TemplateResponse(request=request, name=f"{active}.html", context={
            "title": title, "active": active, "username": unquote(request.headers.get("X-Bellenne-Username", "")),
            "module_prefix": settings.module_prefix, "platform_csrf_token": unquote(request.headers.get("X-Bellenne-Csrf-Token", "")),
            "price_types": PRICE_TYPES, "price_labels": ({"base_price": "Зачёркнутая", "discount_price": "Обычная", "loyalty_price": "Кошелёк / карта"} if policy and policy.price_source == "storefront" else PRICE_LABELS), "collection_errors": COLLECTION_ERRORS, "status_labels": STATUS_LABELS, "reason_labels": REASON_LABELS,
            "policy": policy, "event_labels": {"job_queued": "Задание поставлено в очередь", "sync_start": "Начало задания", "sync_finish": "Задание завершено", "api_error": "Ошибка API", "collection_error": "Ошибка сбора с сайта", "product_discovered": "Обнаружен товар", "mapping_created": "Создана связь", "mapping_conflict": "Неоднозначный артикул", "mapping_unlinked": "Связь разъединена", "price_changed": "Цена изменилась", "price_snapshot_created": "Сохранён снимок цен", "comparison_recalculated": "Сравнение обновлено"}, **context,
        })

    def redirect(path):
        return RedirectResponse(settings.module_prefix + path, status_code=303)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic errors include submitted input; never return secrets on 422.
        return JSONResponse({"detail": "Проверьте значения полей формы или параметры запроса."}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        if "text/html" in request.headers.get("accept", ""):
            return templates.TemplateResponse(request=request, name="error.html", context={"title": "Не удалось выполнить действие", "message": exc.detail, "module_prefix": settings.module_prefix}, status_code=exc.status_code)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "service": "BellenneParity"}

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, owner=Depends(identity), session=Depends(db)):
        # Safe idempotent provisioning of a new account, serialized by DB.
        write_lock(session)
        ensure_owner(session, owner)
        session.commit()
        counts = {}
        for state in ("matched", "mismatch", "mapping_required", "price_unavailable", "stale"):
            counts[state] = catalog(session, owner, status=state, page_size=1)["total"]
        mapped = session.scalar(select(func.count()).select_from(Mapping).where(Mapping.owner_id == owner, Mapping.wb_product_id.is_not(None), Mapping.ozon_product_id.is_not(None)))
        return render(request, session, "overview", "Обзор", counts=counts, mapped=mapped, accounts=[account_view(a) for a in session.scalars(select(Account).where(Account.owner_id == owner))], jobs=list(session.scalars(select(Job).where(Job.owner_id == owner).order_by(Job.id.desc()).limit(10))))

    @app.get("/integrations", response_class=HTMLResponse)
    def integrations(request: Request, owner=Depends(identity), session=Depends(db)):
        write_lock(session)
        ensure_owner(session, owner)
        session.commit()
        active = session.get(ActiveJob, owner)
        return render(request, session, "integrations", "Интеграции", accounts=[account_view(a) for a in session.scalars(select(Account).where(Account.owner_id == owner))], active_job=session.get(Job, active.job_id) if active else None, error_labels=ERRORS, saved=request.query_params.get("saved") == "1")

    @app.post("/integrations/{marketplace}")
    def save_integration(marketplace: str, request: Request, csrf_token: str = Form(...), client_id: str = Form("", max_length=80), api_key: str = Form("", max_length=8192), owner=Depends(identity), session=Depends(db)):
        csrf(request, csrf_token)
        write_lock(session)
        ensure_owner(session, owner)
        require_idle(session, owner)
        current = account(session, owner, marketplace)
        client_id, api_key = client_id.strip(), api_key.strip()
        if marketplace == "ozon" and (not client_id.isdecimal() or int(client_id) <= 0):
            raise HTTPException(422, "Укажите числовой Client ID из кабинета Ozon.")
        if not api_key and not current.secret:
            raise HTTPException(422, "Укажите API-ключ подключения.")
        if marketplace == "ozon" and current.client_id and current.client_id != client_id and not api_key:
            raise HTTPException(422, "При смене Client ID укажите соответствующий новый API-ключ.")
        if api_key or current.client_id != client_id:
            if api_key:
                current.secret = cipher(settings).encrypt(api_key.encode()).decode()
            current.client_id, current.status, current.capabilities = client_id, "not_checked", {}
            current.credential_version += 1
            current.last_error = None
        session.commit()
        return redirect("/integrations?saved=1")

    @app.post("/storefront")
    def save_storefront(request: Request, csrf_token: str = Form(...), page_interval_seconds: int = Form(2, ge=2, le=60), owner=Depends(identity), session=Depends(db)):
        csrf(request, csrf_token)
        write_lock(session)
        policy = ensure_owner(session, owner)
        require_idle(session, owner)
        if policy.price_source != "storefront" or policy.city != "Москва" or policy.price_basis != "loyalty_price":
            policy.context_version += 1
            policy.enabled = False
        policy.price_source, policy.city, policy.price_basis = "storefront", "Москва", "loyalty_price"
        policy.page_interval_seconds = page_interval_seconds
        recalculate(session, owner)
        session.commit()
        return redirect("/integrations?saved=1")

    @app.post("/schedule")
    def save_schedule(request: Request, csrf_token: str = Form(...), daily_time: str = Form(...), timezone_name: str = Form(...), stale_hours: int = Form(24, ge=1, le=8760), enabled: bool = Form(False), owner=Depends(identity), session=Depends(db)):
        csrf(request, csrf_token)
        try:
            daily_time = datetime.strptime(daily_time, "%H:%M").strftime("%H:%M")
            ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError):
            raise HTTPException(422, "Укажите время HH:MM и существующий часовой пояс IANA.") from None
        write_lock(session)
        policy = ensure_owner(session, owner)
        require_idle(session, owner)
        if enabled:
            accounts = list(session.scalars(select(Account).where(Account.owner_id == owner)))
            if policy.price_source == "storefront" and not session.scalar(select(Product.id).where(Product.owner_id == owner).limit(1)):
                raise HTTPException(422, "Сначала импортируйте каталог и проверьте сбор одной карточки.")
            if policy.price_source != "storefront" and any(not a.secret for a in accounts):
                raise HTTPException(422, "Перед включением ежедневной синхронизации сохраните подключения WB и Ozon.")
        policy.daily_time, policy.timezone, policy.enabled, policy.stale_hours = daily_time, timezone_name, enabled, stale_hours
        if enabled and policy.browser_client_id:
            policy.browser_client_id = None
            policy.context_version += 1
        recalculate(session, owner)
        session.commit()
        return redirect("/integrations?saved=1")

    @app.post("/jobs")
    def start_job(request: Request, csrf_token: str = Form(...), marketplace: str = Form(""), kind: str = Form("sync"), collector: str = Form("server"), target_product_id: int | None = Form(None), owner=Depends(identity), session=Depends(db)):
        csrf(request, csrf_token)
        if marketplace not in ("", "wb", "ozon") or kind not in ("sync", "probe", "catalog") or (kind == "probe" and not marketplace):
            raise HTTPException(422, "Выберите маркетплейс и действие.")
        if collector not in ("server", "browser"):
            raise HTTPException(422, "Выберите способ сбора.")
        write_lock(session)
        ensure_owner(session, owner)
        if target_product_id is not None:
            target = session.get(Product, target_product_id)
            if not target or target.owner_id != owner or (marketplace and target.marketplace != marketplace) or kind == "catalog":
                raise HTTPException(404, "Товар для сбора не найден.")
            marketplace = target.marketplace
        if collector == "browser" and kind != "catalog":
            job = browser.start(session, owner, marketplace or None, target_product_id, kind == "probe")
        else:
            if kind != "catalog" and not session.get(ActiveJob, owner):
                policy = session.get(Policy, owner)
                if policy.browser_client_id:
                    policy.context_version += 1
                    policy.browser_client_id = None
                    recalculate(session, owner)
            job = enqueue(session, owner, marketplace or None, kind, target_product_id=target_product_id)
        session.commit()
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"job_id": job.id}, status_code=202)
        return redirect(f"/jobs/{job.id}")

    def own_job(session, owner, job_id):
        job = session.get(Job, job_id)
        if not job or job.owner_id != owner:
            raise HTTPException(404, "Задание не найдено.")
        return job

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: int, owner=Depends(identity), session=Depends(db)):
        job = own_job(session, owner, job_id)
        return {**{key: getattr(job, key) for key in ("id", "status", "phase", "kind", "marketplace", "started_at", "finished_at", "products_received", "products_updated", "prices_updated", "errors_count", "results")},
                "browser": browser.view(session, job) if job.kind == "browser" else None,
                "error_summary": summarize_errors(job.results) or job.error_summary}

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(job_id: int, request: Request, owner=Depends(identity), session=Depends(db)):
        job = own_job(session, owner, job_id)
        return render(request, session, "job", "Синхронизация", job=job, browser_run=browser.view(session, job) if job.kind == "browser" else None, error_summary=summarize_errors(job.results) or job.error_summary)

    @app.post("/api/browser/{job_id}/claim")
    def browser_claim(job_id: int, body: BrowserClaim, request: Request, owner=Depends(identity), session=Depends(db)):
        csrf(request, body.csrf_token)
        write_lock(session)
        value = browser.claim(session, own_job(session, owner, job_id), str(body.client_id), settings.http_timeout)
        if value.get("task"):
            value["task"]["max_attempts"] = settings.retry_attempts
        session.commit()
        return value

    @app.post("/api/browser/{job_id}/receipt")
    def browser_receipt(job_id: int, body: BrowserReceipt, request: Request, owner=Depends(identity), session=Depends(db)):
        csrf(request, body.csrf_token)
        if (body.observation is None) == (body.error_code is None):
            raise HTTPException(422, "Передайте наблюдение или причину остановки.")
        if body.observation and (not body.observed_at or body.observed_at.tzinfo is None or abs((datetime.now(timezone.utc) - body.observed_at).total_seconds()) > settings.http_timeout * 2):
            raise HTTPException(409, "Наблюдение устарело. Расширение повторно прочитает цену текущей карточки.")
        write_lock(session)
        value = browser.receipt(session, own_job(session, owner, job_id), str(body.client_id), body.task_id, body.token,
                                body.observation.model_dump() if body.observation else None, body.error_code)
        session.commit()
        return value

    @app.post("/api/browser/{job_id}/control")
    def browser_popup_control(job_id: int, body: BrowserControl, request: Request, owner=Depends(identity), session=Depends(db)):
        csrf(request, body.csrf_token)
        write_lock(session)
        job = own_job(session, owner, job_id)
        run = session.get(BrowserRun, job.id)
        if not run or run.client_id != str(body.client_id):
            raise HTTPException(409, "Задание связано с другим профилем браузера.")
        browser.control(session, job, body.action)
        value = browser.view(session, job)
        session.commit()
        return value

    @app.post("/browser/{job_id}/control")
    def browser_control(job_id: int, request: Request, action: str = Form(...), csrf_token: str = Form(...), owner=Depends(identity), session=Depends(db)):
        csrf(request, csrf_token)
        write_lock(session)
        browser.control(session, own_job(session, owner, job_id), action)
        session.commit()
        return redirect(f"/jobs/{job_id}")

    @app.get("/browser-extension")
    def browser_download(owner=Depends(identity)):
        return FileResponse(ROOT / "static" / "bellenne-parity-extension.zip", filename="bellenne-parity-extension.zip", media_type="application/zip")

    @app.get("/api/integrations")
    def integration_status(owner=Depends(identity), session=Depends(db)):
        return [account_view(a) for a in session.scalars(select(Account).where(Account.owner_id == owner))]

    @app.get("/products", response_class=HTMLResponse)
    @app.get("/discrepancies", response_class=HTMLResponse)
    @app.get("/mappings", response_class=HTMLResponse)
    def product_list(request: Request, q: str = Query("", max_length=500), exact: bool = False, status: str = "", price_type: str = "", direction: str = "", min_percent: str = Query("", max_length=32), updated_since: str = "", sort: str = "updated", availability: str = "", page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100), owner=Depends(identity), session=Depends(db)):
        if not session.get(Policy, owner):
            return redirect("/")
        active = request.url.path.strip("/")
        try:
            percent = Decimal(min_percent) if min_percent else None
            if percent is not None and (not percent.is_finite() or not 0 <= percent <= 1000000):
                raise ValueError()
        except (InvalidOperation, ValueError):
            raise HTTPException(422, "Укажите неотрицательный процент расхождения.") from None
        if price_type and price_type not in PRICE_TYPES or direction not in ("", "wb_cheaper", "ozon_cheaper"):
            raise HTTPException(422, "Неверный фильтр цены.")
        since = None
        if updated_since:
            try:
                since = datetime.fromisoformat(updated_since).replace(tzinfo=ZoneInfo(session.get(Policy, owner).timezone)).astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                raise HTTPException(422, "Укажите корректную дату обновления.") from None
        filters = dict(q=q, exact=exact, status="mismatch" if active == "discrepancies" else status, price_type=price_type, direction=direction, min_percent=percent, updated_since=since, sort=sort, availability=availability, page=page, page_size=page_size)
        data = catalog(session, owner, **filters)
        return render(request, session, "catalog", {"products": "Товары", "discrepancies": "Расхождения", "mappings": "Сопоставления"}[active], view=active, data=data, filters=filters, updated_since=updated_since,
                      page_link=lambda p: settings.module_prefix + request.url.path + pagination_query(request, p),
                      busy=bool(session.get(ActiveJob, owner)))

    @app.get("/api/products")
    def product_choices(marketplace: str, q: str = Query("", max_length=500), page: int = Query(1, ge=1), owner=Depends(identity), session=Depends(db)):
        if marketplace not in ("wb", "ozon"):
            raise HTTPException(422, "Выберите площадку.")
        query = select(Product).where(Product.owner_id == owner, Product.marketplace == marketplace)
        if q:
            query = query.where(or_(*[field.contains(q, autoescape=True) for field in (Product.name, Product.seller_article, Product.external_id, Product.sku)]))
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        return {"total": total, "page": page, "items": [{"id": p.id, "name": p.name, "article": p.seller_article, "external_id": p.external_id, "sku": p.sku} for p in session.scalars(query.order_by(Product.seller_article, Product.id).offset((page-1)*50).limit(50))]}

    @app.post("/mappings/link")
    def link_mapping(request: Request, wb_product_id: int = Form(...), ozon_product_id: int = Form(...), csrf_token: str = Form(...), owner=Depends(identity), session=Depends(db)):
        csrf(request, csrf_token)
        write_lock(session)
        require_idle(session, owner)
        try:
            manual_link(session, owner, wb_product_id, ozon_product_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        session.commit()
        return redirect("/mappings")

    @app.post("/mappings/{mapping_id}/unlink")
    def unlink_mapping(mapping_id: int, request: Request, csrf_token: str = Form(...), owner=Depends(identity), session=Depends(db)):
        csrf(request, csrf_token)
        write_lock(session)
        require_idle(session, owner)
        mapping = session.get(Mapping, mapping_id)
        if not mapping or mapping.owner_id != owner:
            raise HTTPException(404, "Связь не найдена.")
        unlink(session, mapping)
        log(session, owner, "mapping_unlinked", mapping_id=mapping.id)
        recalculate(session, owner)
        session.commit()
        return redirect("/mappings")

    @app.get("/products/{mapping_id}", response_class=HTMLResponse)
    def product_details(mapping_id: int, request: Request, days: int = Query(30, ge=0, le=90), page: int = Query(1, ge=1), source: str = "current", owner=Depends(identity), session=Depends(db)):
        mapping = session.get(Mapping, mapping_id)
        if not mapping or mapping.owner_id != owner:
            raise HTTPException(404, "Товар не найден.")
        if days not in (0, 7, 30, 90):
            raise HTTPException(422, "Выберите период 7, 30, 90 дней или всю историю.")
        from .models import Snapshot, Comparison
        products = {p.marketplace: p for p in session.scalars(select(Product).where(Product.id.in_([i for i in (mapping.wb_product_id, mapping.ozon_product_id) if i])))}
        prices = {mp: session.get(Snapshot, p.current_snapshot_id) if p.current_snapshot_id else None for mp, p in products.items()}
        policy = session.get(Policy, owner)
        urls = {}
        for mp, product in products.items():
            try:
                urls[mp] = product_url(mp, product.external_id, product.sku)
            except CollectionError:
                urls[mp] = None
            snapshot = prices[mp]
            if snapshot and policy.price_source == "storefront" and (snapshot.source_kind != "storefront" or snapshot.context_version != policy.context_version):
                prices[mp] = None
        contexts = list(session.execute(select(Snapshot.source_kind, Snapshot.context_version).where(Snapshot.product_id.in_([p.id for p in products.values()])).distinct()))
        choices = {"current": "Текущие условия · кошелёк / карта", "seller_api": "История Seller API"}
        choices.update({f"storefront:{v}": f"Витрина · условия #{v}" for kind, v in contexts if kind == "storefront" and v != policy.context_version})
        if source not in choices:
            raise HTTPException(422, "Выберите источник истории.")
        source_kind = "seller_api" if source == "seller_api" or (source == "current" and policy.price_source != "storefront") else "storefront"
        context_version = 0 if source_kind == "seller_api" else policy.context_version if source == "current" else int(source.split(":")[1])
        comparisons = list(session.scalars(select(Comparison).where(Comparison.mapping_id == mapping_id)))
        display_status = mapping.status
        if len(products) == 2 and all(p.present for p in products.values()) and any(is_stale(p, session.get(Account, p.account_id), session.get(Policy, owner), now()) for p in products.values()):
            display_status = "stale"
            comparisons = [SimpleNamespace(price_type=c.price_type, status="stale", signed_difference=None, difference_percent=None, direction=None) for c in comparisons]
        return render(request, session, "details", "История цен", mapping=mapping, display_status=display_status, products=products, prices=prices, comparisons=comparisons, history=history(session, mapping, days, page, source_kind=source_kind, context_version=context_version), days=days,
                      source=source, history_sources=choices, urls=urls, busy=bool(session.get(ActiveJob, owner)),
                      history_price_labels=PRICE_LABELS if source_kind == "seller_api" else {"base_price": "Зачёркнутая", "discount_price": "Обычная", "loyalty_price": "Кошелёк / карта"})

    @app.get("/observations/{snapshot_id}")
    def observation_image(snapshot_id: int, owner=Depends(identity), session=Depends(db)):
        snapshot = session.scalar(select(Snapshot).join(Product, Snapshot.product_id == Product.id).where(Snapshot.id == snapshot_id, Product.owner_id == owner))
        if not snapshot or not snapshot.evidence_key:
            raise HTTPException(404, "Снимок блока цены недоступен.")
        import re
        if not re.fullmatch(r"[0-9a-f]{32}\.jpg", snapshot.evidence_key):
            raise HTTPException(404, "Снимок недоступен.")
        path = settings.data_dir / "observations" / snapshot.evidence_key
        if not path.is_file():
            raise HTTPException(404, "Файл снимка недоступен.")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/journal", response_class=HTMLResponse)
    def journal(request: Request, page: int = Query(1, ge=1), job_id: int | None = None, product_id: int | None = None, owner=Depends(identity), session=Depends(db)):
        query = select(Event).where(Event.owner_id == owner)
        if job_id is not None:
            query = query.where(Event.job_id == job_id)
        if product_id is not None:
            query = query.where(Event.product_id == product_id)
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        events = list(session.scalars(query.order_by(Event.id.desc()).offset((page-1)*100).limit(100)))
        return render(request, session, "journal", "Журнал", events=events, page=page, pages=max(1,(total+99)//100),
                      page_link=lambda p: pagination_query(request, p))

    return app


app = create_app()
