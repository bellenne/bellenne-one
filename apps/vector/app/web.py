from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, BackgroundTasks, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, cast, func, select
from sqlalchemy.exc import IntegrityError

from app.models import (
    Account,
    Campaign,
    DailyStat,
    Product,
    ProductComment,
    SchedulerSetting,
    SyncRun,
    User,
    utcnow,
)
from app.services.analytics import (
    available_dimensions,
    build_analytics_report,
)
from app.services.auth import AuthManager
from app.services.token_vault import TokenVault

TEMPLATES_DIR = Path(__file__).parent / "templates"
MODULE_PREFIX = os.getenv("MODULE_PREFIX", "").rstrip("/")


def _shared_template_context(request: Request) -> dict[str, str]:
    return {
        "module_prefix": MODULE_PREFIX,
        "platform_csrf_token": getattr(
            request.state, "platform_csrf_token", ""
        ),
    }


templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR),
    context_processors=[_shared_template_context],
)
router = APIRouter()


def _owned_account(session, user_id: int) -> Account | None:
    return session.scalar(
        select(Account).where(Account.user_id == user_id).limit(1)
    )


def _current_user_id(request: Request) -> int:
    return int(request.state.user_id)


def _redirect(path: str, message: str, kind: str = "success"):
    path = _module_url(path)
    separator = "&" if "?" in path else "?"
    return RedirectResponse(
        f"{path}{separator}message={quote(message)}&kind={quote(kind)}",
        status_code=303,
    )


def _module_url(path: str) -> str:
    if not MODULE_PREFIX or path.startswith(f"{MODULE_PREFIX}/") or path == MODULE_PREFIX:
        return path
    return f"{MODULE_PREFIX}{path}"


def _safe_percent(numerator, denominator) -> float:
    return float(numerator or 0) / float(denominator or 0) if denominator else 0


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def _data_page_url(
    page: int,
    *,
    date_from: date | None,
    date_to: date | None,
    group: str,
    query: str,
) -> str:
    parameters: dict[str, str | int] = {"page": page}
    if date_from:
        parameters["date_from"] = date_from.isoformat()
    if date_to:
        parameters["date_to"] = date_to.isoformat()
    if group:
        parameters["group"] = group
    if query:
        parameters["query"] = query
    return _module_url(f"/data?{urlencode(parameters)}")


def _safe_data_return(value: str) -> str:
    if value == _module_url("/data") or value.startswith(
        f"{_module_url('/data')}?"
    ):
        return value
    return _module_url("/data")


@router.get("/healthz")
def healthz():
    return {"status": "ok", "service": "BellenneVector"}


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if request.state.user_id is not None:
        return RedirectResponse(_module_url("/"), status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="auth.html",
        context={
            "request": request,
            "mode": "register",
            "message": request.query_params.get("message"),
            "kind": request.query_params.get("kind", "error"),
            "next": _safe_next(request.query_params.get("next")),
        },
    )


@router.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    next_path: str = Form("/", alias="next"),
):
    if request.state.user_id is not None:
        return RedirectResponse(_module_url("/"), status_code=303)
    auth: AuthManager = request.app.state.auth
    normalized_username = auth.normalize_username(username)
    if not auth.username_is_valid(normalized_username):
        return _redirect(
            "/register",
            "Логин: от 3 до 64 символов — буквы, цифры, точка, @, +, - или _",
            "error",
        )
    if not auth.password_is_valid(password):
        return _redirect(
            "/register",
            "Пароль должен содержать от 8 до 128 символов",
            "error",
        )
    if password != password_confirm:
        return _redirect("/register", "Пароли не совпадают", "error")

    with request.app.state.session_factory() as session:
        user = User(
            username=normalized_username,
            password_hash=auth.hash_password(password),
            last_login_at=utcnow(),
        )
        session.add(user)
        try:
            session.flush()
            legacy_account = session.scalar(
                select(Account)
                .where(Account.user_id.is_(None))
                .order_by(Account.id)
                .limit(1)
            )
            if legacy_account is not None:
                legacy_account.user_id = user.id
            session.commit()
        except IntegrityError:
            session.rollback()
            return _redirect(
                "/register",
                "Пользователь с таким логином уже существует",
                "error",
            )
        user_id = user.id

    response = _redirect(
        _safe_next(next_path),
        "Аккаунт создан. Добро пожаловать!",
    )
    auth.set_session_cookie(response, user_id)
    return response


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.state.user_id is not None:
        return RedirectResponse(_module_url("/"), status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="auth.html",
        context={
            "request": request,
            "mode": "login",
            "message": request.query_params.get("message"),
            "kind": request.query_params.get("kind", "error"),
            "next": _safe_next(request.query_params.get("next")),
        },
    )


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_path: str = Form("/", alias="next"),
):
    auth: AuthManager = request.app.state.auth
    normalized_username = auth.normalize_username(username)
    with request.app.state.session_factory() as session:
        user = session.scalar(
            select(User).where(User.username == normalized_username)
        )
        if user is None or not auth.verify_password(
            password,
            user.password_hash,
        ):
            return _redirect(
                "/login",
                "Неверный логин или пароль",
                "error",
            )
        user.last_login_at = utcnow()
        session.commit()
        user_id = user.id

    response = _redirect(_safe_next(next_path), "Вход выполнен")
    auth.set_session_cookie(response, user_id)
    return response


@router.post("/logout")
def logout(request: Request):
    response = _redirect("/login", "Вы вышли из аккаунта")
    request.app.state.auth.clear_session_cookie(response)
    return response


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    group: str = Query(""),
    subject: str = Query(""),
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        account = _owned_account(session, _current_user_id(request))
        context = {
            "request": request,
            "active": "dashboard",
            "account": account,
            "message": request.query_params.get("message"),
            "kind": request.query_params.get("kind", "success"),
            "totals": None,
            "last_run": None,
            "date_min": None,
            "date_max": None,
            "report": None,
            "groups": [],
            "subjects": [],
            "filters": {
                "date_from": date_from,
                "date_to": date_to,
                "group": group,
                "subject": subject,
            },
        }
        if account:
            date_min, date_max = session.execute(
                select(
                    func.min(DailyStat.stat_date),
                    func.max(DailyStat.stat_date),
                ).where(DailyStat.account_id == account.id)
            ).one()
            context["date_min"] = date_min
            context["date_max"] = date_max
            context["last_run"] = session.scalar(
                select(SyncRun)
                .where(SyncRun.account_id == account.id)
                .order_by(SyncRun.started_at.desc())
                .limit(1)
            )
            context["groups"], context["subjects"] = available_dimensions(
                session,
                account.id,
            )
            if date_min and date_max:
                default_from = max(date_min, date_max - timedelta(days=29))
                begin = date_from or default_from
                end = date_to or date_max
                if begin > end:
                    return _redirect(
                        "/",
                        "Дата начала не может быть позже даты окончания",
                        "error",
                    )
                report = build_analytics_report(
                    session,
                    account.id,
                    begin,
                    end,
                    group=group,
                    subject=subject,
                )
                context["report"] = report
                context["totals"] = report["totals"]
                context["filters"]["date_from"] = begin
                context["filters"]["date_to"] = end
                context["quick_ranges"] = {
                    "week": max(date_min, date_max - timedelta(days=6)),
                    "month": default_from,
                    "all": date_min,
                    "end": date_max,
                }
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=context,
        )


@router.get("/cabinet", response_class=HTMLResponse)
def cabinet(request: Request):
    with request.app.state.session_factory() as session:
        account = _owned_account(session, _current_user_id(request))
        return templates.TemplateResponse(
            request=request,
            name="cabinet.html",
            context={
                "request": request,
                "active": "cabinet",
                "account": account,
                "message": request.query_params.get("message"),
                "kind": request.query_params.get("kind", "success"),
            },
        )


@router.post("/cabinet")
def save_cabinet(
    request: Request,
    name: str = Form("Основной кабинет"),
    token: str = Form(""),
):
    clean_name = name.strip() or "Основной кабинет"
    vault: TokenVault = request.app.state.vault
    with request.app.state.session_factory() as session:
        user_id = _current_user_id(request)
        account = _owned_account(session, user_id)
        if account is None:
            account = Account(name=clean_name, user_id=user_id)
            session.add(account)
            session.flush()
            session.add(
                SchedulerSetting(
                    account_id=account.id,
                    timezone_name=request.app.state.settings.app_timezone,
                )
            )
        else:
            account.name = clean_name

        if token.strip():
            account.encrypted_token = vault.encrypt(token)
            account.token_hint = vault.hint(token)
        elif not account.encrypted_token:
            return _redirect(
                "/cabinet",
                "Для нового кабинета укажите API-ключ",
                "error",
            )
        session.commit()
    return _redirect("/cabinet", "Настройки кабинета сохранены")


@router.post("/cabinet/test")
async def test_cabinet(request: Request):
    vault: TokenVault = request.app.state.vault
    with request.app.state.session_factory() as session:
        account = _owned_account(session, _current_user_id(request))
        if account is None or not account.encrypted_token:
            return _redirect(
                "/cabinet",
                "Сначала сохраните API-ключ",
                "error",
            )
        token = vault.decrypt(account.encrypted_token)

    try:
        async with request.app.state.client_factory(token) as client:
            await client.ping()
            campaigns = await client.list_campaigns()
    except Exception as exc:
        return _redirect(
            "/cabinet",
            f"Проверка не пройдена: {exc}",
            "error",
        )
    return _redirect(
        "/cabinet",
        f"Подключение работает. Найдено кампаний: {len(campaigns)}",
    )


@router.get("/scheduler", response_class=HTMLResponse)
def scheduler_page(request: Request):
    with request.app.state.session_factory() as session:
        account = _owned_account(session, _current_user_id(request))
        setting = None
        runs = []
        next_run = None
        if account:
            setting = session.scalar(
                select(SchedulerSetting).where(
                    SchedulerSetting.account_id == account.id
                )
            )
            runs = session.scalars(
                select(SyncRun)
                .where(SyncRun.account_id == account.id)
                .order_by(SyncRun.started_at.desc())
                .limit(30)
            ).all()
            next_run = request.app.state.scheduler.next_run(account.id)
        yesterday = datetime.now(
            ZoneInfo(request.app.state.settings.app_timezone)
        ).date() - timedelta(days=1)
        return templates.TemplateResponse(
            request=request,
            name="scheduler.html",
            context={
                "request": request,
                "active": "scheduler",
                "account": account,
                "setting": setting,
                "runs": runs,
                "next_run": next_run,
                "yesterday": yesterday,
                "default_from": yesterday - timedelta(days=2),
                "message": request.query_params.get("message"),
                "kind": request.query_params.get("kind", "success"),
            },
        )


@router.post("/scheduler")
def save_scheduler(
    request: Request,
    enabled: str | None = Form(None),
    run_time: str = Form("05:15"),
    timezone_name: str = Form("Europe/Moscow"),
    lookback_days: int = Form(3),
):
    try:
        parsed_time = time.fromisoformat(run_time)
        ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        return _redirect(
            "/scheduler",
            "Проверьте время и часовой пояс",
            "error",
        )
    if not 1 <= lookback_days <= 31:
        return _redirect(
            "/scheduler",
            "Глубина повторной загрузки — от 1 до 31 дня",
            "error",
        )

    with request.app.state.session_factory() as session:
        account = _owned_account(session, _current_user_id(request))
        if account is None:
            return _redirect(
                "/cabinet",
                "Сначала настройте кабинет",
                "error",
            )
        setting = session.scalar(
            select(SchedulerSetting).where(
                SchedulerSetting.account_id == account.id
            )
        )
        if setting is None:
            setting = SchedulerSetting(account_id=account.id)
            session.add(setting)
        setting.enabled = enabled == "on"
        setting.run_time = parsed_time
        setting.timezone_name = timezone_name
        setting.lookback_days = lookback_days
        session.commit()

    request.app.state.scheduler.reload()
    return _redirect("/scheduler", "Расписание обновлено")


async def _run_collection_safely(
    request: Request,
    account_id: int,
    begin: date,
    end: date,
) -> None:
    try:
        await request.app.state.collector.collect(
            account_id,
            begin,
            end,
            trigger="manual",
        )
    except Exception:
        # Collector records the full safe error text in sync_runs.
        return


@router.post("/scheduler/run")
def run_now(
    request: Request,
    background_tasks: BackgroundTasks,
    date_from: date = Form(...),
    date_to: date = Form(...),
):
    if date_from > date_to:
        return _redirect(
            "/scheduler",
            "Дата начала не может быть позже даты окончания",
            "error",
        )
    with request.app.state.session_factory() as session:
        account = _owned_account(session, _current_user_id(request))
        if account is None or not account.encrypted_token:
            return _redirect(
                "/cabinet",
                "Сначала сохраните API-ключ",
                "error",
            )
        account_id = account.id
    background_tasks.add_task(
        _run_collection_safely,
        request,
        account_id,
        date_from,
        date_to,
    )
    return _redirect(
        "/scheduler",
        "Сбор запущен в фоне. Статус появится в журнале.",
    )


@router.get("/data", response_class=HTMLResponse)
def data_page(
    request: Request,
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    group: str = Query(""),
    query: str = Query(""),
    page: int = Query(1, ge=1),
):
    page_size = 50
    with request.app.state.session_factory() as session:
        account = _owned_account(session, _current_user_id(request))
        if account is None:
            return templates.TemplateResponse(
                request=request,
                name="data.html",
                context={
                    "request": request,
                    "active": "data",
                    "account": None,
                    "rows": [],
                    "products": [],
                    "comments": [],
                    "groups": [],
                    "page": 1,
                    "pages": 0,
                    "pagination": {},
                    "current_page_url": "/data",
                    "filters": {},
                    "message": request.query_params.get("message"),
                    "kind": request.query_params.get("kind", "success"),
                },
            )

        base = (
            select(DailyStat, Product, Campaign)
            .outerjoin(
                Product,
                (Product.account_id == DailyStat.account_id)
                & (Product.nm_id == DailyStat.nm_id),
            )
            .outerjoin(
                Campaign,
                (Campaign.account_id == DailyStat.account_id)
                & (Campaign.advert_id == DailyStat.advert_id),
            )
            .where(DailyStat.account_id == account.id)
        )
        count_statement = (
            select(func.count(DailyStat.id))
            .outerjoin(
                Product,
                (Product.account_id == DailyStat.account_id)
                & (Product.nm_id == DailyStat.nm_id),
            )
            .outerjoin(
                Campaign,
                (Campaign.account_id == DailyStat.account_id)
                & (Campaign.advert_id == DailyStat.advert_id),
            )
            .where(DailyStat.account_id == account.id)
        )
        conditions = []
        if date_from:
            conditions.append(DailyStat.stat_date >= date_from)
        if date_to:
            conditions.append(DailyStat.stat_date <= date_to)
        if group.strip():
            conditions.append(Product.report_group == group.strip())
        if query.strip():
            pattern = f"%{query.strip()}%"
            conditions.append(
                (Product.name.ilike(pattern))
                | (Campaign.name.ilike(pattern))
                | (cast(DailyStat.nm_id, String).ilike(pattern))
                | (cast(DailyStat.advert_id, String).ilike(pattern))
            )
        if conditions:
            base = base.where(*conditions)
            count_statement = count_statement.where(*conditions)

        total = int(session.scalar(count_statement) or 0)
        pages = (total + page_size - 1) // page_size
        page = min(page, max(pages, 1))
        current_page_url = _data_page_url(
            page,
            date_from=date_from,
            date_to=date_to,
            group=group,
            query=query,
        )
        if pages:
            first_page = max(1, page - 2)
            last_page = min(pages, first_page + 4)
            first_page = max(1, last_page - 4)
            pagination = {
                "previous": (
                    _data_page_url(
                        page - 1,
                        date_from=date_from,
                        date_to=date_to,
                        group=group,
                        query=query,
                    )
                    if page > 1
                    else None
                ),
                "next": (
                    _data_page_url(
                        page + 1,
                        date_from=date_from,
                        date_to=date_to,
                        group=group,
                        query=query,
                    )
                    if page < pages
                    else None
                ),
                "items": [
                    {
                        "number": item_page,
                        "url": _data_page_url(
                            item_page,
                            date_from=date_from,
                            date_to=date_to,
                            group=group,
                            query=query,
                        ),
                        "current": item_page == page,
                    }
                    for item_page in range(first_page, last_page + 1)
                ],
            }
        else:
            pagination = {"previous": None, "next": None, "items": []}

        comment_rows = session.execute(
            select(ProductComment, Product)
            .outerjoin(
                Product,
                (Product.account_id == ProductComment.account_id)
                & (Product.nm_id == ProductComment.nm_id),
            )
            .where(ProductComment.account_id == account.id)
            .order_by(ProductComment.updated_at.desc())
        ).all()
        comments = [
            {
                "id": comment.id,
                "nm_id": comment.nm_id,
                "comment": comment.comment,
                "updated_at": comment.updated_at,
                "product_name": product.name if product else "",
            }
            for comment, product in comment_rows
        ]
        comments_by_nm = {
            int(comment["nm_id"]): comment for comment in comments
        }
        result_rows = session.execute(
            base.order_by(
                DailyStat.stat_date.desc(),
                DailyStat.nm_id,
                DailyStat.advert_id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        rows = []
        for stat, product, campaign in result_rows:
            rows.append(
                {
                    "date": stat.stat_date,
                    "group": product.report_group if product else "",
                    "nm_id": stat.nm_id,
                    "product": product.name if product else "",
                    "advert_id": stat.advert_id,
                    "campaign": campaign.name if campaign else "",
                    "views": stat.views,
                    "clicks": stat.clicks,
                    "spend": float(stat.spend),
                    "atbs": stat.atbs,
                    "orders": stat.orders,
                    "revenue": float(stat.revenue),
                    "ctr": _safe_percent(stat.clicks, stat.views),
                    "cpc": (
                        float(stat.spend) / stat.clicks
                        if stat.clicks
                        else 0
                    ),
                    "drr": _safe_percent(stat.spend, stat.revenue),
                    "comment": comments_by_nm.get(int(stat.nm_id)),
                }
            )
        products = session.scalars(
            select(Product)
            .where(Product.account_id == account.id)
            .order_by(Product.report_group, Product.name, Product.nm_id)
            .limit(500)
        ).all()
        groups = session.scalars(
            select(Product.report_group)
            .where(Product.account_id == account.id)
            .distinct()
            .order_by(Product.report_group)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="data.html",
            context={
                "request": request,
                "active": "data",
                "account": account,
                "rows": rows,
                "products": products,
                "comments": comments,
                "groups": groups,
                "page": page,
                "pages": pages,
                "pagination": pagination,
                "current_page_url": current_page_url,
                "total": total,
                "filters": {
                    "date_from": date_from,
                    "date_to": date_to,
                    "group": group,
                    "query": query,
                },
                "message": request.query_params.get("message"),
                "kind": request.query_params.get("kind", "success"),
            },
        )


@router.post("/products/group")
def update_product_group(
    request: Request,
    product_id: int = Form(...),
    report_group: str = Form(...),
):
    clean_group = report_group.strip()
    if not clean_group:
        return _redirect("/data", "Название группы не может быть пустым", "error")
    with request.app.state.session_factory() as session:
        account = _owned_account(session, _current_user_id(request))
        product = (
            session.scalar(
                select(Product).where(
                    Product.id == product_id,
                    Product.account_id == account.id,
                )
            )
            if account is not None
            else None
        )
        if product is None:
            return _redirect("/data", "Товар не найден", "error")
        product.report_group = clean_group[:200]
        product.group_is_manual = True
        session.commit()
    return _redirect("/data", "Группа товара обновлена")


@router.post("/comments")
def save_product_comment(
    request: Request,
    nm_id: int = Form(...),
    comment: str = Form(""),
    return_to: str = Form("/data"),
):
    destination = _safe_data_return(return_to)
    if nm_id <= 0:
        return _redirect(destination, "Артикул должен быть положительным числом", "error")
    clean_comment = comment.strip()
    if len(clean_comment) > 4000:
        return _redirect(destination, "Комментарий не может быть длиннее 4000 символов", "error")

    with request.app.state.session_factory() as session:
        account = _owned_account(session, _current_user_id(request))
        if account is None:
            return _redirect("/cabinet", "Сначала настройте кабинет", "error")
        saved_comment = session.scalar(
            select(ProductComment).where(
                ProductComment.account_id == account.id,
                ProductComment.nm_id == nm_id,
            )
        )
        if not clean_comment:
            if saved_comment is None:
                return _redirect(
                    destination,
                    "Введите текст комментария",
                    "error",
                )
            session.delete(saved_comment)
            session.commit()
            return _redirect(destination, f"Комментарий к артикулу {nm_id} удалён")

        if saved_comment is None:
            saved_comment = ProductComment(
                account_id=account.id,
                nm_id=nm_id,
                comment=clean_comment,
            )
            session.add(saved_comment)
            message = f"Комментарий к артикулу {nm_id} добавлен"
        else:
            saved_comment.comment = clean_comment
            saved_comment.updated_at = utcnow()
            message = f"Комментарий к артикулу {nm_id} обновлён"
        session.commit()
    return _redirect(destination, message)


@router.get("/export.xlsx")
def export_excel(
    request: Request,
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
):
    with request.app.state.session_factory() as session:
        account = _owned_account(session, _current_user_id(request))
        if account is None:
            return _redirect("/cabinet", "Сначала настройте кабинет", "error")
        bounds = session.execute(
            select(
                func.min(DailyStat.stat_date),
                func.max(DailyStat.stat_date),
            ).where(DailyStat.account_id == account.id)
        ).one()
        default_to = bounds[1] or date.today()
        default_from = bounds[0] or default_to

    begin = date_from or default_from
    end = date_to or default_to
    try:
        payload, filename = request.app.state.exporter.build(
            account.id,
            begin,
            end,
        )
    except ValueError as exc:
        return _redirect("/data", str(exc), "error")
    encoded_name = quote(filename)
    return StreamingResponse(
        iter([payload]),
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{encoded_name}"
            )
        },
    )
