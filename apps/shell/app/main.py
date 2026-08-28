from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


BASE_DIR = Path(__file__).parent

app = FastAPI(title="BellenneOne", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def shared_context(request: Request, *, active: str, title: str) -> dict[str, object]:
    return {
        "request": request,
        "active": active,
        "title": title,
        "username": unquote(request.headers.get("X-Bellenne-Username", ""))
        or "Пользователь",
        "platform_csrf_token": unquote(
            request.headers.get("X-Bellenne-Csrf-Token", "")
        ),
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "BellenneOne"}


@app.get("/", response_class=HTMLResponse)
def overview(request: Request) -> HTMLResponse:
    if not request.headers.get("X-Bellenne-User-Id"):
        return JSONResponse(
            {"detail": "Bellenne identity header is required"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    return templates.TemplateResponse(
        request=request,
        name="overview.html",
        context=shared_context(request, active="overview", title="Обзор"),
    )


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request) -> HTMLResponse:
    if not request.headers.get("X-Bellenne-User-Id"):
        return JSONResponse(
            {"detail": "Bellenne identity header is required"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context=shared_context(request, active="settings", title="Настройки"),
    )

