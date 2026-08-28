from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
import os
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.config import Settings
from app.database import build_engine, build_session_factory, init_database
from app.models import Account, User
from app.services.auth import AuthManager
from app.services.collector import Collector
from app.services.exporter import WorkbookExporter
from app.services.scheduler import SchedulerService
from app.services.token_vault import TokenVault
from app.services.wildberries import WildberriesClient
from app.web import router


MODULE_PREFIX = os.getenv("MODULE_PREFIX", "").rstrip("/")
EXTERNAL_AUTH_ENABLED = os.getenv("EXTERNAL_AUTH_ENABLED", "false").casefold() in {
    "1",
    "true",
    "yes",
    "on",
}


def create_app(
    settings: Settings | None = None,
    client_factory: Callable[[str], WildberriesClient] | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    vault = TokenVault(settings.data_dir)
    auth = AuthManager(
        settings.data_dir,
        secure_cookie=settings.session_https_only,
    )
    client_factory = client_factory or (
        lambda token: WildberriesClient(token, settings)
    )
    collector = Collector(session_factory, vault, client_factory)
    exporter = WorkbookExporter(session_factory)
    scheduler = SchedulerService(
        session_factory,
        collector,
        settings.app_timezone,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_database(engine)
        collector.normalize_product_groups()
        await scheduler.start()
        yield
        await scheduler.shutdown()
        engine.dispose()

    app = FastAPI(
        title="BellenneVector",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.vault = vault
    app.state.auth = auth
    app.state.client_factory = client_factory
    app.state.collector = collector
    app.state.exporter = exporter
    app.state.scheduler = scheduler

    @app.middleware("http")
    async def require_authentication(request: Request, call_next):
        request.state.user_id = None
        request.state.username = None
        request.state.platform_csrf_token = ""
        path = request.url.path

        if EXTERNAL_AUTH_ENABLED:
            if path == "/healthz" or path.startswith("/static/"):
                return await call_next(request)
            try:
                external_user_id = int(
                    request.headers.get("X-Bellenne-User-Id", "")
                )
            except ValueError:
                external_user_id = 0
            if external_user_id > 0:
                username = unquote(
                    request.headers.get("X-Bellenne-Username", "")
                ) or f"bellenne-{external_user_id}"
                with session_factory() as session:
                    user = session.scalar(
                        select(User).where(
                            User.external_user_id == external_user_id
                        )
                    )
                    if user is None:
                        user = session.scalar(
                            select(User).where(User.username == username)
                        )
                        if user is None:
                            user = User(
                                username=username,
                                password_hash="external-auth",
                                external_user_id=external_user_id,
                            )
                            session.add(user)
                            session.flush()
                            legacy_account = session.scalar(
                                select(Account)
                                .where(Account.user_id.is_(None))
                                .order_by(Account.id)
                                .limit(1)
                            )
                            if legacy_account is not None:
                                legacy_account.user_id = user.id
                        else:
                            user.external_user_id = external_user_id
                        session.commit()
                        session.refresh(user)
                    request.state.user_id = user.id
                    request.state.username = user.username
                    request.state.platform_csrf_token = unquote(
                        request.headers.get("X-Bellenne-Csrf-Token", "")
                    )
                return await call_next(request)
            return RedirectResponse("/login", status_code=303)

        session_token = request.cookies.get(auth.cookie_name)
        user_id = auth.verify_session(session_token)
        if user_id is not None:
            with session_factory() as session:
                user = session.scalar(
                    select(User).where(User.id == user_id)
                )
                if user is not None:
                    request.state.user_id = user.id
                    request.state.username = user.username

        is_public = (
            path in {"/login", "/register", "/healthz"}
            or path.startswith("/static/")
        )
        if request.state.user_id is None and not is_public:
            destination = path
            if request.url.query:
                destination = f"{destination}?{request.url.query}"
            response = RedirectResponse(
                f"/login?next={quote(destination, safe='')}",
                status_code=303,
            )
            if session_token:
                auth.clear_session_cookie(response)
            return response
        return await call_next(request)

    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )
    app.include_router(router)
    return app


app = create_app()
