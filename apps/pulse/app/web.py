from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.bootstrap import ensure_demo_data
from app.models import (
    Marketplace,
    MarketplaceAccount,
    MarketplaceProduct,
    PlanMode,
    ProductGroup,
    User,
    WBSummaryMetric,
)
from app.security import csrf_matches, encrypt_credentials, hash_password, new_csrf_token, verify_password
from app.services.exporter import build_report_workbook
from app.services.metrics import PLAN_INPUT_FIELDS, compare_value, decimal
from app.services.planning import (
    daily_plan,
    load_plan,
    parse_plan_payload,
    plan_for_range,
    plan_monthly_values,
    save_plan,
    validate_plan,
)
from app.services.product_groups import merge_product_groups
from app.services.reporting import all_groups, available_accounts, available_groups, report_summary
from app.services.sync import integration_for, rebuild_user_totals, slugify, sync_account
from app.services.wb_summary import MAX_WB_SUMMARY_BYTES, import_wb_summary


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MODULE_PREFIX = os.getenv("MODULE_PREFIX", "").rstrip("/")


def format_number(value, digits: int = 0) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:,.{digits}f}".replace(",", " ")


def format_money(value) -> str:
    return "—" if value is None else f"{format_number(value)} ₽"


def format_percent(value, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.{digits}f}%"


def format_date(value, fmt: str = "%d.%m.%Y") -> str:
    return value.strftime(fmt) if hasattr(value, "strftime") else "—"


def format_month_year(value) -> str:
    months = (
        "январь",
        "февраль",
        "март",
        "апрель",
        "май",
        "июнь",
        "июль",
        "август",
        "сентябрь",
        "октябрь",
        "ноябрь",
        "декабрь",
    )
    return f"{months[value.month - 1]} {value.year}" if hasattr(value, "month") else "—"


def format_input_number(value) -> str:
    if value is None:
        return "0"
    rounded = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    result = format(rounded, "f").rstrip("0").rstrip(".")
    return result or "0"


templates.env.filters.update(
    number=format_number,
    money=format_money,
    percent=format_percent,
    date=format_date,
    month_year=format_month_year,
    input_number=format_input_number,
)


def get_db(request: Request) -> Session:
    return request.app.state.session_factory()


def current_user(request: Request, session: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = session.get(User, int(user_id))
    return user if user and user.is_active else None


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = new_csrf_token()
        request.session["csrf_token"] = token
    return token


def verify_csrf(request: Request, form) -> None:
    if not csrf_matches(request.session.get("csrf_token"), form.get("csrf_token")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token mismatch")


def flash(request: Request, message: str, kind: str = "info") -> None:
    messages = request.session.setdefault("flashes", [])
    messages.append({"message": message, "kind": kind})
    request.session["flashes"] = messages[-5:]


def pop_flashes(request: Request) -> list[dict[str, str]]:
    return request.session.pop("flashes", [])


def template_context(request: Request, user: User | None, active_page: str, **extra):
    return {
        "request": request,
        "user": user,
        "csrf_token": ensure_csrf(request),
        "active_page": active_page,
        "flashes": pop_flashes(request),
        "today": date.today(),
        "module_prefix": MODULE_PREFIX,
        **extra,
    }


def require_user_or_redirect(request: Request, session: Session):
    user = current_user(request, session)
    if user is None:
        return None, RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return user, None


def parse_iso_date(value: str | None, fallback: date) -> date:
    try:
        return date.fromisoformat(value) if value else fallback
    except ValueError:
        return fallback


def parse_optional_int(value: str | None) -> int | None:
    try:
        parsed = int(value or 0)
        return parsed or None
    except ValueError:
        return None


def combined_monthly_plan(session: Session, user_id: int, period: date, marketplace: str, group_id: int | None = None):
    if marketplace != "ALL":
        return plan_monthly_values(load_plan(session, user_id, period, marketplace), group_id)
    combined = {field: Decimal("0") for field in PLAN_INPUT_FIELDS}
    for market in (Marketplace.WB.value, Marketplace.OZON.value):
        values = plan_monthly_values(load_plan(session, user_id, period, market), group_id)
        for field in PLAN_INPUT_FIELDS:
            combined[field] += decimal(values.get(field))
    from app.services.metrics import derived_values

    return derived_values(combined)


router = APIRouter()


@router.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "BellennePulse"}


@router.get("/internal/auth")
async def internal_auth(request: Request):
    """Validate the shared Bellenne session for the reverse proxy."""
    with get_db(request) as session:
        user = current_user(request, session)
        if user is None:
            return Response(status_code=status.HTTP_401_UNAUTHORIZED)
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={
                "X-Bellenne-User-Id": str(user.id),
                "X-Bellenne-Username": quote(user.username, safe=""),
                "X-Bellenne-Csrf-Token": quote(
                    str(request.session.get("csrf_token", "")), safe=""
                ),
            },
        )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    with get_db(request) as session:
        if current_user(request, session):
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=template_context(request, None, "login", title="Вход"),
    )


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    with get_db(request) as session:
        if current_user(request, session):
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context=template_context(request, None, "register", title="Регистрация"),
    )


@router.post("/register")
async def register(request: Request):
    form = await request.form()
    verify_csrf(request, form)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    password_confirm = str(form.get("password_confirm", ""))
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9_.-]{3,80}", username):
        flash(request, "Логин: 3–80 символов; разрешены буквы, цифры, точка, дефис и подчёркивание.", "error")
        return RedirectResponse("/register", status_code=status.HTTP_303_SEE_OTHER)
    if len(password) < 8:
        flash(request, "Пароль должен содержать не менее 8 символов.", "error")
        return RedirectResponse("/register", status_code=status.HTTP_303_SEE_OTHER)
    if password != password_confirm:
        flash(request, "Пароли не совпадают.", "error")
        return RedirectResponse("/register", status_code=status.HTTP_303_SEE_OTHER)
    with get_db(request) as session:
        if session.scalar(select(User).where(User.username == username)):
            flash(request, "Пользователь с таким логином уже существует.", "error")
            return RedirectResponse("/register", status_code=status.HTTP_303_SEE_OTHER)
        user = User(username=username, password_hash=hash_password(password))
        session.add(user)
        session.commit()
        session.refresh(user)
        if request.app.state.settings.demo_mode:
            await ensure_demo_data(session, user, request.app.state.settings)
        request.session.clear()
        request.session["user_id"] = user.id
        request.session["csrf_token"] = new_csrf_token()
        flash(request, "Аккаунт создан. Демонстрационные кабинеты можно удалить в разделе «Кабинеты».", "success")
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/login")
async def login(request: Request):
    form = await request.form()
    verify_csrf(request, form)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    with get_db(request) as session:
        user = session.scalar(select(User).where(User.username == username))
        if not user or not user.is_active or not verify_password(password, user.password_hash):
            flash(request, "Неверный логин или пароль.", "error")
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        request.session.clear()
        request.session["user_id"] = user.id
        request.session["csrf_token"] = new_csrf_token()
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request):
    form = await request.form()
    verify_csrf(request, form)
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    today = date.today()
    date_to = parse_iso_date(request.query_params.get("date_to"), today)
    date_from = parse_iso_date(request.query_params.get("date_from"), date_to.replace(day=1))
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    marketplace = request.query_params.get("marketplace", "ALL").upper()
    if marketplace not in {"ALL", Marketplace.WB.value, Marketplace.OZON.value}:
        marketplace = "ALL"
    account_id = parse_optional_int(request.query_params.get("account_id"))
    group_id = parse_optional_int(request.query_params.get("product_group_id"))

    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        summary, series = report_summary(session, user.id, date_from, date_to, marketplace, account_id, group_id)
        plan_month = combined_monthly_plan(session, user.id, date_to, marketplace, group_id)
        plan_range = plan_for_range(plan_month, date_to, date_from, date_to)
        chart_metric_keys = (
            "ordered_units",
            "ordered_amount",
            "average_order_value",
            "buyout_units",
            "buyout_amount",
            "ad_spend",
            "ad_bonus_spend",
            "drr_buyouts_total",
            "drr_orders_total",
            "drr_buyouts_cash",
            "drr_orders_cash",
        )
        for item in series:
            item_plan = daily_plan(plan_month, date_to, item["date"]) if item["date"].month == date_to.month else {}
            for metric_key in chart_metric_keys:
                plan_value = item_plan.get(metric_key)
                actual_value = item.get(metric_key)
                item[f"plan_{metric_key}"] = float(decimal(plan_value)) if plan_value is not None else None
                if metric_key in {"ordered_units", "buyout_units"}:
                    item[metric_key] = int(decimal(actual_value))
                else:
                    item[metric_key] = float(decimal(actual_value)) if actual_value is not None else None
            item["date_label"] = item["date"].strftime("%d.%m")
        cards = [
            {"key": "ordered_units", "label": "Заказано (шт)", "value": summary["ordered_units"], "plan": plan_range["ordered_units"], "format": "number", "tone": "orders"},
            {"key": "ordered_amount", "label": "Сумма заказов (руб)", "value": summary["ordered_amount"], "plan": plan_range["ordered_amount"], "format": "money", "tone": "revenue"},
            {"key": "average_order_value", "label": "Средний чек заказа", "value": summary["average_order_value"], "plan": plan_range["average_order_value"], "format": "money", "tone": "revenue"},
            {"key": "buyout_units", "label": "Выкуплено (шт)", "value": summary["buyout_units"], "plan": plan_range["buyout_units"], "format": "number", "tone": "buyout"},
            {"key": "buyout_amount", "label": "Сумма выкупов (руб)", "value": summary["buyout_amount"], "plan": plan_range["buyout_amount"], "format": "money", "tone": "buyout"},
            {"key": "ad_spend", "label": "РК Денег", "value": summary["ad_spend"], "plan": plan_range["ad_spend"], "format": "money", "tone": "warning"},
            {"key": "ad_bonus_spend", "label": "РК Бонусов", "value": summary["ad_bonus_spend"], "plan": plan_range["ad_bonus_spend"], "format": "money", "tone": "warning"},
            {"key": "drr_buyouts_total", "label": "Общий ДРРв (%)", "value": summary["drr_buyouts_total"], "plan": plan_range["drr_buyouts_total"], "format": "percent", "tone": "warning"},
            {"key": "drr_orders_total", "label": "Общий ДРРз (%)", "value": summary["drr_orders_total"], "plan": plan_range["drr_orders_total"], "format": "percent", "tone": "warning"},
            {"key": "drr_buyouts_cash", "label": "Денег ДРРв (%)", "value": summary["drr_buyouts_cash"], "plan": plan_range["drr_buyouts_cash"], "format": "percent", "tone": "warning"},
            {"key": "drr_orders_cash", "label": "Денег ДРРз (%)", "value": summary["drr_orders_cash"], "plan": plan_range["drr_orders_cash"], "format": "percent", "tone": "warning"},
            {"key": "gross_profit", "label": "Валовая прибыль", "value": None, "plan": plan_range["gross_profit"], "format": "money", "tone": "neutral", "note": "Факт поступит из внешнего сервиса"},
        ]
        for card in cards:
            if card["key"] in chart_metric_keys:
                card["actual_key"] = card["key"]
                card["plan_key"] = f"plan_{card['key']}"
            card["completion"] = compare_value(card["value"], card["plan"])
        chart_cards = [card for card in cards if card.get("actual_key")]
        secondary_cards = [card for card in cards if not card.get("actual_key")]
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=template_context(
                request,
                user,
                "dashboard",
                title="Обзор",
                cards=cards,
                chart_cards=chart_cards,
                secondary_cards=secondary_cards,
                series=series,
                series_json=json.dumps(
                    series,
                    ensure_ascii=False,
                    default=lambda value: value.isoformat()
                    if isinstance(value, date)
                    else float(value)
                    if isinstance(value, Decimal)
                    else str(value),
                ),
                date_from=date_from,
                date_to=date_to,
                marketplace=marketplace,
                account_id=account_id,
                group_id=group_id,
                accounts=available_accounts(session, user.id),
                groups=available_groups(session, user.id),
                summary=summary,
                plan_range=plan_range,
            ),
        )


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        accounts = available_accounts(session, user.id)
        products = list(
            session.scalars(
                select(MarketplaceProduct)
                .join(MarketplaceAccount)
                .where(MarketplaceAccount.user_id == user.id)
                .order_by(MarketplaceProduct.name)
            )
        )
        summary_info = {
            account_id: {"rows": rows_count, "last_date": last_date}
            for account_id, rows_count, last_date in session.execute(
                select(
                    WBSummaryMetric.account_id,
                    func.count(WBSummaryMetric.id),
                    func.max(WBSummaryMetric.metric_date),
                )
                .where(WBSummaryMetric.user_id == user.id)
                .group_by(WBSummaryMetric.account_id)
            )
        }
        return templates.TemplateResponse(
            request=request,
            name="accounts.html",
            context=template_context(
                request,
                user,
                "accounts",
                title="Кабинеты",
                accounts=accounts,
                products=products,
                groups=all_groups(session, user.id),
                summary_info=summary_info,
                default_timezone=request.app.state.settings.timezone,
                yesterday=date.today() - timedelta(days=1),
            ),
        )


@router.post("/accounts")
async def create_account(request: Request):
    form = await request.form()
    verify_csrf(request, form)
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        marketplace = str(form.get("marketplace", "")).upper()
        name = str(form.get("name", "")).strip()
        if marketplace not in {Marketplace.WB.value, Marketplace.OZON.value} or not name:
            flash(request, "Укажите название и маркетплейс.", "error")
            return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)
        credentials = (
            {"api_token": str(form.get("wb_api_token", "")).strip(), "advert_token": str(form.get("wb_advert_token", "")).strip()}
            if marketplace == Marketplace.WB.value
            else {"client_id": str(form.get("ozon_client_id", "")).strip(), "api_key": str(form.get("ozon_api_key", "")).strip()}
        )
        if not any(credentials.values()):
            flash(request, "Укажите API-данные кабинета.", "error")
            return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)
        timezone_name = str(form.get("timezone", request.app.state.settings.timezone)).strip()
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            timezone_name = request.app.state.settings.timezone
        try:
            sync_time = datetime.strptime(str(form.get("sync_time", "06:15")), "%H:%M").time()
        except ValueError:
            sync_time = datetime.strptime("06:15", "%H:%M").time()
        account = MarketplaceAccount(
            user_id=user.id,
            name=name,
            marketplace=marketplace,
            encrypted_credentials=encrypt_credentials(request.app.state.credential_cipher, credentials),
            schedule_enabled=form.get("schedule_enabled") == "on",
            sync_time=sync_time,
            timezone=timezone_name,
        )
        session.add(account)
        session.commit()
        flash(request, "Кабинет добавлен. Проверьте подключение перед первым сбором.", "success")
    return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/validate")
async def validate_account(account_id: int, request: Request):
    form = await request.form()
    verify_csrf(request, form)
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        account = session.scalar(select(MarketplaceAccount).where(MarketplaceAccount.id == account_id, MarketplaceAccount.user_id == user.id))
        if not account:
            raise HTTPException(404)
        from app.security import decrypt_credentials

        credentials = decrypt_credentials(request.app.state.credential_cipher, account.encrypted_credentials)
        try:
            integration = integration_for(account, credentials, request.app.state.settings)
            ok, message = await integration.validate()
        except Exception as exc:
            ok, message = False, str(exc)
        account.last_sync_status = "success" if ok else "error"
        account.last_sync_message = message
        session.commit()
        flash(request, message, "success" if ok else "error")
    return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/wb-summary")
async def import_account_wb_summary(account_id: int, request: Request):
    form = await request.form()
    verify_csrf(request, form)
    upload = form.get("summary_file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        flash(request, "Выберите XLSX-файл сводного отчёта WB.", "error")
        return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)
    filename = Path(upload.filename).name
    if Path(filename).suffix.casefold() != ".xlsx":
        await upload.close()
        flash(request, "Поддерживается только формат XLSX.", "error")
        return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)
    content = await upload.read(MAX_WB_SUMMARY_BYTES + 1)
    await upload.close()
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        account = session.scalar(
            select(MarketplaceAccount).where(
                MarketplaceAccount.id == account_id,
                MarketplaceAccount.user_id == user.id,
            )
        )
        if not account:
            raise HTTPException(404)
        try:
            try:
                account_zone = ZoneInfo(account.timezone or request.app.state.settings.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                account_zone = ZoneInfo(request.app.state.settings.timezone)
            through_date = datetime.now(account_zone).date() - timedelta(days=1)
            result = import_wb_summary(
                session,
                account,
                content,
                filename,
                through_date,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            flash(request, str(exc), "error")
        else:
            skipped = (
                f" Строк после {through_date:%d.%m.%Y} пропущено: "
                f"{result.skipped_after_cutoff}."
                if result.skipped_after_cutoff
                else ""
            )
            flash(
                request,
                f"Сводный отчёт WB загружен: {result.rows_imported} дней, "
                f"{result.first_date:%d.%m.%Y}–{result.last_date:%d.%m.%Y}.{skipped}",
                "success",
            )
    return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/sync")
async def run_account_sync(account_id: int, request: Request):
    form = await request.form()
    verify_csrf(request, form)
    target_date = parse_iso_date(str(form.get("target_date", "")), date.today() - timedelta(days=1))
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        account = session.scalar(select(MarketplaceAccount).where(MarketplaceAccount.id == account_id, MarketplaceAccount.user_id == user.id))
        if not account:
            raise HTTPException(404)
        try:
            account_zone = ZoneInfo(account.timezone or request.app.state.settings.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            account_zone = ZoneInfo(request.app.state.settings.timezone)
        latest_date = datetime.now(account_zone).date() - timedelta(days=1)
        if target_date > latest_date:
            flash(request, "Сегодняшний день не собирается. Выберите вчера или более раннюю дату.", "warning")
            return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)
        run = await sync_account(session, account, target_date, request.app.state.credential_cipher, request.app.state.settings)
        flash(request, run.message or "Сбор завершён", "success" if run.status == "success" else ("warning" if run.status == "warning" else "error"))
    return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/toggle")
async def toggle_account(account_id: int, request: Request):
    form = await request.form()
    verify_csrf(request, form)
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        account = session.scalar(select(MarketplaceAccount).where(MarketplaceAccount.id == account_id, MarketplaceAccount.user_id == user.id))
        if not account:
            raise HTTPException(404)
        account.is_active = not account.is_active
        session.commit()
        flash(request, "Состояние кабинета обновлено.", "success")
    return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/delete")
async def delete_account(account_id: int, request: Request):
    form = await request.form()
    verify_csrf(request, form)
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        account = session.scalar(select(MarketplaceAccount).where(MarketplaceAccount.id == account_id, MarketplaceAccount.user_id == user.id))
        if not account:
            raise HTTPException(404)
        session.delete(account)
        session.commit()
        flash(request, "Кабинет и связанные метрики удалены.", "warning")
    return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/product-groups")
async def create_product_group(request: Request):
    form = await request.form()
    verify_csrf(request, form)
    name = str(form.get("name", "")).strip()
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        if not name:
            flash(request, "Введите название типа товара.", "error")
        elif session.scalar(select(ProductGroup).where(ProductGroup.user_id == user.id, ProductGroup.name == name)):
            flash(request, "Такой тип товара уже существует.", "warning")
        else:
            session.add(ProductGroup(user_id=user.id, name=name, code=slugify(name)))
            session.commit()
            flash(request, "Тип товара добавлен.", "success")
    return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/product-groups/merge")
async def merge_categories(request: Request):
    form = await request.form()
    verify_csrf(request, form)
    target_group_id = parse_optional_int(str(form.get("target_group_id", "")))
    source_group_ids = [
        group_id
        for value in form.getlist("source_group_ids")
        if (group_id := parse_optional_int(str(value))) is not None
    ]
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        if target_group_id is None:
            flash(request, "Выберите итоговую категорию.", "error")
            return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)
        try:
            result = merge_product_groups(
                session,
                user.id,
                target_group_id,
                source_group_ids,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            flash(request, str(exc), "error")
        else:
            flash(
                request,
                f"Категории объединены в «{result.target_name}»: "
                f"{result.categories_merged} катег., {result.products_moved} товаров. "
                f"История и планы пересчитаны.",
                "success",
            )
    return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/product-groups/{group_id}/toggle")
async def toggle_product_group(group_id: int, request: Request):
    form = await request.form()
    verify_csrf(request, form)
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        group = session.scalar(
            select(ProductGroup).where(
                ProductGroup.id == group_id,
                ProductGroup.user_id == user.id,
            )
        )
        if not group:
            raise HTTPException(404)
        group.is_active = not group.is_active
        rebuilt = rebuild_user_totals(session, user.id)
        session.commit()
        state = "включена" if group.is_active else "отключена"
        flash(
            request,
            f"Категория «{group.name}» {state}. Пересчитано дневных итогов: {rebuilt}.",
            "success",
        )
    return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/products/{product_id}/group")
async def map_product_group(product_id: int, request: Request):
    form = await request.form()
    verify_csrf(request, form)
    group_id = parse_optional_int(str(form.get("product_group_id", "")))
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        product = session.scalar(
            select(MarketplaceProduct)
            .join(MarketplaceAccount)
            .where(MarketplaceProduct.id == product_id, MarketplaceAccount.user_id == user.id)
        )
        group = session.scalar(select(ProductGroup).where(ProductGroup.id == group_id, ProductGroup.user_id == user.id)) if group_id else None
        if not product or not group:
            raise HTTPException(404)
        product.product_group_id = group.id
        session.commit()
        flash(request, "Товар перенесён в выбранный тип.", "success")
    return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/products/{product_id}/toggle")
async def toggle_product(product_id: int, request: Request):
    form = await request.form()
    verify_csrf(request, form)
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        product = session.scalar(
            select(MarketplaceProduct)
            .join(MarketplaceAccount)
            .where(MarketplaceProduct.id == product_id, MarketplaceAccount.user_id == user.id)
        )
        if not product:
            raise HTTPException(404)
        product.is_active = not product.is_active
        session.commit()
        state = "включён" if product.is_active else "отключён"
        flash(request, f"Товар «{product.name}» {state}. Изменение применяется при следующем сборе даты.", "success")
    return RedirectResponse("/accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/plans", response_class=HTMLResponse)
async def plans_page(request: Request):
    today = date.today()
    period_raw = request.query_params.get("period", today.strftime("%Y-%m"))
    period = parse_iso_date(f"{period_raw}-01", today.replace(day=1))
    marketplace = request.query_params.get("marketplace", Marketplace.WB.value).upper()
    if marketplace not in {Marketplace.WB.value, Marketplace.OZON.value}:
        marketplace = Marketplace.WB.value
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        groups = available_groups(session, user.id)
        plan = load_plan(session, user.id, period, marketplace)
        totals = {field: Decimal("0") for field in PLAN_INPUT_FIELDS}
        group_values = {group.id: {field: Decimal("0") for field in PLAN_INPUT_FIELDS} for group in groups}
        allocations = {group.id: Decimal("0") for group in groups}
        if plan:
            for item in plan.values:
                if item.scope_key == "total":
                    totals[item.metric_key] = item.monthly_value
                elif item.product_group_id in group_values:
                    group_values[item.product_group_id][item.metric_key] = item.monthly_value
            for item in plan.allocations:
                allocations[item.product_group_id] = item.percentage
        return templates.TemplateResponse(
            request=request,
            name="plans.html",
            context=template_context(
                request,
                user,
                "plans",
                title="Обновление плана",
                period=period,
                marketplace=marketplace,
                plan=plan,
                mode=plan.mode if plan else PlanMode.TOTAL.value,
                fields=PLAN_INPUT_FIELDS,
                totals=totals,
                group_values=group_values,
                allocations=allocations,
                groups=groups,
            ),
        )


@router.post("/plans")
async def update_plan(request: Request):
    raw_form = await request.form()
    verify_csrf(request, raw_form)
    form = {key: str(value) for key, value in raw_form.items()}
    period = parse_iso_date(f"{form.get('period', date.today().strftime('%Y-%m'))}-01", date.today().replace(day=1))
    marketplace = form.get("marketplace", Marketplace.WB.value).upper()
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        groups = available_groups(session, user.id)
        mode, totals, group_values, allocations = parse_plan_payload(form, groups)
        errors = validate_plan(mode, allocations, group_values)
        if marketplace not in {Marketplace.WB.value, Marketplace.OZON.value}:
            errors.append("Выберите маркетплейс.")
        if errors:
            for error in errors:
                flash(request, error, "error")
            return RedirectResponse(f"/plans?period={period:%Y-%m}&marketplace={marketplace}", status_code=status.HTTP_303_SEE_OTHER)
        save_plan(session, user.id, period, marketplace, mode, totals, group_values, allocations)
        when = "будущий месяц" if period > date.today().replace(day=1) else "текущий месяц"
        flash(request, f"План сохранён на {when}. Дневные и недельные значения пересчитаны автоматически.", "success")
    return RedirectResponse(f"/plans?period={period:%Y-%m}&marketplace={marketplace}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    today = date.today()
    date_to = parse_iso_date(request.query_params.get("date_to"), today)
    date_from = parse_iso_date(request.query_params.get("date_from"), date_to.replace(day=1))
    marketplace = request.query_params.get("marketplace", "ALL").upper()
    account_id = parse_optional_int(request.query_params.get("account_id"))
    group_id = parse_optional_int(request.query_params.get("product_group_id"))
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        summary, series = report_summary(session, user.id, date_from, date_to, marketplace, account_id, group_id)
        return templates.TemplateResponse(
            request=request,
            name="reports.html",
            context=template_context(
                request,
                user,
                "reports",
                title="Отчёты",
                date_from=date_from,
                date_to=date_to,
                marketplace=marketplace,
                account_id=account_id,
                group_id=group_id,
                accounts=available_accounts(session, user.id),
                groups=available_groups(session, user.id),
                summary=summary,
                series=series,
            ),
        )


@router.get("/reports/export.xlsx")
async def export_report(request: Request):
    period_raw = request.query_params.get("period", date.today().strftime("%Y-%m"))
    period = parse_iso_date(f"{period_raw}-01", date.today().replace(day=1))
    account_id = parse_optional_int(request.query_params.get("account_id"))
    with get_db(request) as session:
        user, redirect = require_user_or_redirect(request, session)
        if redirect:
            return redirect
        if account_id:
            account = session.scalar(select(MarketplaceAccount).where(MarketplaceAccount.id == account_id, MarketplaceAccount.user_id == user.id))
            if not account:
                raise HTTPException(404)
        payload = build_report_workbook(session, user.id, period, account_id)
    filename = f"BellennePulse_{period:%Y-%m}_Pererabotka-test.xlsx"
    return Response(
        payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/metrics")
async def metrics_api(request: Request):
    today = date.today()
    date_to = parse_iso_date(request.query_params.get("date_to"), today)
    date_from = parse_iso_date(request.query_params.get("date_from"), date_to.replace(day=1))
    marketplace = request.query_params.get("marketplace", "ALL").upper()
    with get_db(request) as session:
        user = current_user(request, session)
        if not user:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        summary, series = report_summary(session, user.id, date_from, date_to, marketplace)

        def serializable(value):
            if isinstance(value, Decimal):
                return float(value)
            if isinstance(value, date):
                return value.isoformat()
            return value

        return {
            "summary": {key: serializable(value) for key, value in summary.items()},
            "series": [{key: serializable(value) for key, value in row.items()} for row in series],
        }
