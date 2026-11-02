"""CSRF double-submit как app-level зависимость (ADR-10, ADR-22, фаза 3b).

До фазы 3b проверка висела на admin_router: подроутер, подключённый мимо него, её обходил
(F1 ревью фазы 1.5). Теперь `csrf_guard` стоит в `FastAPI(dependencies=[...])` и проверяет все
небезопасные методы приложения. Исключения - только то, что не живёт на cookie-сессии:
  * безопасные методы (GET, HEAD, OPTIONS, TRACE);
  * /internal/v1/* - сервис-к-сервису, защищено HMAC (правило 10 про /admin сюда не относится);
  * /health и статика админки - без форм.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Final

from fastapi import Request
from starlette.responses import Response

from app.admin.cookies import CSRF_COOKIE, SESSION_MAX_AGE, set_cookie
from app.common.errors import Forbidden
from app.core.admin_access import verify_csrf

CSRF_FIELD: Final = "_csrf"
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_EXEMPT_PREFIXES: Final = ("/internal/v1/", "/admin/static/")
_EXEMPT_PATHS: Final = frozenset({"/health"})
_SHAPE: Final = re.compile(r"[0-9a-f]{64}")


def new_csrf_value() -> str:
    """SHA-256 от 32 случайных байт, hex (ADR-10)."""
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def current_csrf_value(request: Request) -> tuple[str, bool]:
    """Значение для формы и флаг «cookie надо выставить».

    Действующую cookie переиспользуем, чтобы несколько открытых вкладок не
    инвалидировали токены друг друга.
    """
    existing = request.cookies.get(CSRF_COOKIE)
    if existing is not None and _SHAPE.fullmatch(existing):
        return existing, False
    return new_csrf_value(), True


def attach_csrf_cookie(response: Response, csrf_value: str, *, secure: bool) -> None:
    # HttpOnly=False: токен читает JS/HTMX; SameSite=Strict (ADR-10).
    set_cookie(
        response,
        CSRF_COOKIE,
        csrf_value,
        max_age=SESSION_MAX_AGE,
        secure=secure,
        httponly=False,
        samesite="Strict",
    )


def is_exempt(method: str, path: str) -> bool:
    """Нужна ли проверка CSRF для запроса. Исключения перечислены выше и только они."""
    if method in _SAFE_METHODS:
        return True
    return path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)


async def csrf_guard(request: Request) -> None:
    """Зависимость приложения: double-submit для небезопасных методов вне исключений.

    Несовпадение -> Forbidden("csrf_invalid") -> 403 {"error": "csrf_invalid"}. Форма читается
    через request.form(), которую Starlette кеширует: обработчик с Form-параметрами получит тот же
    разбор, повторного чтения тела нет.
    """
    if is_exempt(request.method, request.url.path):
        return
    form = await request.form()
    raw = form.get(CSRF_FIELD)
    form_value = raw if isinstance(raw, str) else None
    if not verify_csrf(request.cookies.get(CSRF_COOKIE), form_value):
        raise Forbidden("csrf_invalid")
