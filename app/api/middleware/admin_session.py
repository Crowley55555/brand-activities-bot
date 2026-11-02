from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Final
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.admin.cookies import SESSION_COOKIE, clear_cookie
from app.admin.session import is_secret_usable, load_session
from app.config import get_settings
from app.core.admin_access import get_active_admin
from app.db.unit_of_work import UnitOfWork

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

LOGIN_PATH: Final = "/admin/login"
LOGOUT_PATH: Final = "/admin/logout"
STATIC_PREFIX: Final = "/admin/static/"
_PUBLIC_PATHS: Final = frozenset({LOGIN_PATH, LOGOUT_PATH})


def _is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


class AdminSessionMiddleware:
    """Правило 10 / ADR-9: любой /admin/* кроме login, logout и static - только
    с валидной подписанной сессией живого админа.

    Порядок проверок:
      1. ключ подписи не настроен -> 503 (fail-closed, а не подпись пустым ключом);
      2. нет cookie -> 302 /admin/login?next=...;
      3. подпись/срок (8ч) неверны -> то же + сброс cookie;
      4. админа нет в БД / is_active=FALSE / deleted_at IS NOT NULL ->
         сброс cookie + 302 /admin/login (подпись валидна, но сессия мертва);
      5. иначе request.state.admin = AdminRef(...) и запрос идёт дальше.

    Это ASGI-middleware, а не зависимость: он видит и маршруты, которых нет
    (404 не раскрывает структуру админки неавторизованным), и любые будущие
    роутеры под /admin независимо от того, как их подключили.
    """

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        path: str = scope["path"]
        if not _is_admin_path(path) or path.startswith(STATIC_PREFIX):
            await self._app(scope, receive, send)
            return

        if scope_type == "websocket":
            # WebSocket-маршрутов под /admin нет; если появятся - без cookie-
            # проверки их пускать нельзя, поэтому закрываем (policy violation).
            await send({"type": "websocket.close", "code": 1008})
            return

        rejection = await self._reject_or_authenticate(scope, receive, path)
        if rejection is not None:
            await rejection(scope, receive, send)
            return
        await self._app(scope, receive, send)

    async def _reject_or_authenticate(
        self, scope: Scope, receive: Receive, path: str
    ) -> Response | None:
        settings = get_settings()
        signing_key = settings.admin_session_secret
        if not is_secret_usable(signing_key):
            # Предупреждение один раз при старте (lifespan в app/main.py);
            # здесь не логируем, чтобы запросы без ключа не засоряли журнал.
            return JSONResponse(
                {"error": "admin_auth_not_configured"}, status_code=503
            )

        if path in _PUBLIC_PATHS:
            return None

        request = Request(scope, receive)
        secure = settings.cookie_secure
        raw_cookie = request.cookies.get(SESSION_COOKIE)
        if not raw_cookie:
            return _to_login(request, with_next=True, reset_cookie=False, secure=secure)

        payload = load_session(raw_cookie, signing_key)
        if payload is None:
            return _to_login(request, with_next=True, reset_cookie=True, secure=secure)

        async with UnitOfWork(scope["app"].state.session_factory) as uow:
            admin = await get_active_admin(uow.repositories.admins, payload.admin_id)
        if admin is None:
            return _to_login(request, with_next=False, reset_cookie=True, secure=secure)

        scope.setdefault("state", {})["admin"] = admin
        return None


def _to_login(
    request: Request, *, with_next: bool, reset_cookie: bool, secure: bool
) -> Response:
    location = LOGIN_PATH
    # next имеет смысл только для страниц (GET); после логина POST-маршрут
    # по GET дал бы 405.
    if with_next and request.method in ("GET", "HEAD"):
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        location = f"{LOGIN_PATH}?next={quote(target, safe='/')}"
    response = RedirectResponse(location, status_code=302)
    if reset_cookie:
        clear_cookie(
            response, SESSION_COOKIE, secure=secure, httponly=True, samesite="Lax"
        )
    return response
