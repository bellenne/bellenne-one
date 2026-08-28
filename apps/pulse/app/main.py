from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.bootstrap import initialize_database
from app.config import Settings, get_settings
from app.database import build_engine, build_session_factory
from app.scheduler import build_scheduler
from app.security import credential_cipher
from app.web import router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await initialize_database(engine, session_factory, settings)
        scheduler = None
        if settings.scheduler_enabled:
            scheduler = build_scheduler(app)
            scheduler.start()
        try:
            yield
        finally:
            if scheduler:
                scheduler.shutdown(wait=False)
            engine.dispose()

    app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.credential_cipher = credential_cipher(settings.app_secret_key, settings.credentials_encryption_key)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_secret_key,
        session_cookie="bellennepulse_session",
        same_site="lax",
        https_only=settings.secure_cookies,
        max_age=60 * 60 * 12,
    )
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(router)
    return app


app = create_app()
