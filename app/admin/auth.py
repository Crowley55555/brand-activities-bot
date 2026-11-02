from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from starlette.datastructures import FormData
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.admin.cookies import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    clear_cookie,
    set_cookie,
)
from app.admin.session import sign_session
from app.admin.templating import render_page
from app.api.deps import get_clock, get_password_hasher, get_uow
from app.api.middleware.csrf import attach_csrf_cookie, current_csrf_value
from app.config import Settings, get_settings
from app.core.admin_access import authenticate, start_session, touch_last_login
from app.core.ports.clock import ClockPort
from app.core.ports.password_hasher import PasswordHasherPort
from app.db.unit_of_work import UnitOfWork

# Правило 2: в логи - только события и внутренние id, ни логинов, ни паролей.
logger = logging.getLogger("app.admin.auth")

router = APIRouter()

DEFAULT_NEXT = "/admin/"
_NEVER_NEXT = frozenset({"/admin/login", "/admin/logout"})
_MAX_NEXT_LENGTH = 2048


def safe_next(raw: str) -> str:
    """Куда вернуть после логина: только внутрь /admin (нет open redirect)."""
    if not raw or len(raw) > _MAX_NEXT_LENGTH or not raw.isprintable():
        return DEFAULT_NEXT
    if "\\" in raw:
        return DEFAULT_NEXT
    path = raw.split("?", 1)[0]
    if path != "/admin" and not path.startswith("/admin/"):
        return DEFAULT_NEXT
    if path.rstrip("/") in _NEVER_NEXT:
        return DEFAULT_NEXT
    return raw


def _form_str(form: FormData, key: str) -> str:
    value = form.get(key)
    return value if isinstance(value, str) else ""


def _login_page(
    request: Request,
    settings: Settings,
    *,
    next_url: str,
    failed: bool,
    status_code: int,
) -> Response:
    csrf_value, is_new = current_csrf_value(request)
    response = render_page(
        "login.html",
        title="Вход",
        status_code=status_code,
        csrf=csrf_value,
        next_url=next_url,
        failed=failed,
    )
    if is_new:
        attach_csrf_cookie(response, csrf_value, secure=settings.cookie_secure)
    return response


@router.get("/login")
async def login_form(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    next_url: Annotated[str, Query(alias="next")] = "",
) -> Response:
    return _login_page(
        request, settings, next_url=safe_next(next_url), failed=False, status_code=200
    )


@router.post("/login")
async def login_submit(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    hasher: Annotated[PasswordHasherPort, Depends(get_password_hasher)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> Response:
    # CSRF уже проверен app-level зависимостью csrf_guard (ADR-10, ADR-22).
    form = await request.form()
    login_in = _form_str(form, "login").strip()
    pw_in = _form_str(form, "password")
    next_url = safe_next(_form_str(form, "next"))

    repo = uow.repositories.admins
    admin = await authenticate(repo, login_in, pw_in, hasher)
    if admin is None:
        logger.warning("admin_login_failed", extra={"event": "admin_login_failed"})
        # 200, а не 401: по RFC 9110 401 обязан нести WWW-Authenticate, а
        # Basic-вызов открыл бы браузерное окно. Неудачу в access-логе
        # отличает 200 от 302 успешного входа.
        return _login_page(
            request, settings, next_url=next_url, failed=True, status_code=200
        )

    now = clock.now()
    await touch_last_login(repo, admin.id, now)
    await uow.commit()  # F2: 302 с cookie уходит только после фиксации входа
    cookie_value = sign_session(start_session(admin, now), settings.admin_session_secret)

    response = RedirectResponse(next_url, status_code=302)
    set_cookie(
        response,
        SESSION_COOKIE,
        cookie_value,
        max_age=SESSION_MAX_AGE,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="Lax",
    )
    logger.info(
        "admin_login_ok",
        extra={"event": "admin_login_ok", "admin_id": int(admin.id)},
    )
    return response


@router.post("/logout")
async def logout(
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Выход из админки (I-3 фазы 10b-2): POST под CSRF вместо прежнего GET.

    CSRF double-submit уже проверил app-level `csrf_guard` (ADR-22) до вызова
    хэндлера; здесь только сбрасываем сессионную cookie и редиректим на вход.
    GET-эндпоинт (см. ниже) отвечает 405 — внешний `<img src=/admin/logout>`
    больше не разлогинивает админа.
    """
    response = RedirectResponse("/admin/login", status_code=302)
    clear_cookie(
        response,
        SESSION_COOKIE,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="Lax",
    )
    return response


@router.get("/logout")
async def logout_get_rejected() -> Response:
    """GET /admin/logout → 405 (I-3 фазы 10b-2).

    Явный GET-обработчик нужен, чтобы в HTTP-ответе был `Allow: POST` — иначе
    FastAPI отдал бы 404 (роута нет) или 405 без Allow (роут есть, метод не тот).
    Cookie при 405 не трогается: внешний `<img src=/admin/logout>` или
    случайный GET админа ничего не меняет.
    """
    return JSONResponse(
        {"error": "method_not_allowed"},
        status_code=405,
        headers={"Allow": "POST"},
    )
