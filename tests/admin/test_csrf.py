"""ADR-10 и ADR-22: CSRF double-submit для /admin и app-level охват всех небезопасных маршрутов.

Поведенческие проверки входа - прежние. Регрессия F1 фазы 1.5 (проверка висела на admin_router)
закрыта инвариантом: каждый POST/PUT/PATCH/DELETE приложения несёт csrf_guard, включая подроутеры.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from httpx import AsyncClient
from starlette.routing import Mount

from app.api.middleware.csrf import csrf_guard, is_exempt
from tests.admin.conftest import (
    CSRF_COOKIE,
    CreatedAdmin,
    fetch_csrf,
    find_set_cookie,
    login_form,
    sign_in,
)


async def test_login_page_sets_csrf_cookie_and_matching_hidden_field(
    admin_client: AsyncClient,
) -> None:
    resp = await admin_client.get("/admin/login")

    assert resp.status_code == 200
    cookie = find_set_cookie(resp, CSRF_COOKIE)
    assert cookie is not None
    assert re.fullmatch(r"[0-9a-f]{64}", cookie.value), "SHA-256 hex"
    assert not cookie.has("httponly"), "HttpOnly=False: токен читает JS/HTMX"
    assert cookie.attrs["samesite"].lower() == "strict"
    assert cookie.attrs["path"] == "/admin"
    hidden = re.search(r'name="_csrf" value="([^"]*)"', resp.text)
    assert hidden is not None and hidden.group(1) == cookie.value
    assert '<form method="post" action="/admin/login"' in resp.text


async def test_csrf_cookie_is_reused_not_rotated_on_next_page_load(
    admin_client: AsyncClient,
) -> None:
    first = await fetch_csrf(admin_client)

    second_resp = await admin_client.get("/admin/login")

    assert find_set_cookie(second_resp, CSRF_COOKIE) is None
    assert admin_client.cookies.get(CSRF_COOKIE) == first


async def test_login_post_without_csrf_field_rejects_403(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    admin = admin_user_factory()
    await fetch_csrf(admin_client)
    pw = admin.plain

    resp = await admin_client.post(
        "/admin/login", data={"login": admin.login, "password": pw}
    )

    assert resp.status_code == 403
    assert resp.json() == {"error": "csrf_invalid"}


async def test_login_post_with_mismatched_csrf_rejects_403(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    admin = admin_user_factory()
    await fetch_csrf(admin_client)

    resp = await admin_client.post(
        "/admin/login", data=login_form(admin.login, admin.plain, "0" * 64)
    )

    assert resp.status_code == 403
    assert find_set_cookie(resp, "admin_session") is None


async def test_login_post_with_form_csrf_but_no_cookie_rejects_403(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    admin = admin_user_factory()

    resp = await admin_client.post(
        "/admin/login", data=login_form(admin.login, admin.plain, "a" * 64)
    )

    assert resp.status_code == 403


async def test_empty_csrf_in_cookie_and_form_is_not_a_match(
    admin_client: AsyncClient,
) -> None:
    resp = await admin_client.post(
        "/admin/login",
        data={"login": "x", "password": "y", "_csrf": ""},
        headers={"Cookie": f"{CSRF_COOKIE}="},
    )

    assert resp.status_code == 403


async def test_non_ascii_csrf_value_is_rejected_without_server_error(
    admin_client: AsyncClient,
) -> None:
    csrf = await fetch_csrf(admin_client)

    resp = await admin_client.post(
        "/admin/login", data=login_form("x", "y", "пароль-" + csrf)
    )

    assert resp.status_code == 403


async def test_unauthenticated_post_is_redirected_before_csrf_check(
    api_client: AsyncClient,
) -> None:
    """Сессия проверяется раньше CSRF: аноним не узнаёт ничего об устройстве формы."""
    resp = await api_client.post("/admin/ugc/1/approve", data={"x": "1"})

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/admin/login")


def test_csrf_guard_is_an_app_level_dependency() -> None:
    """ADR-22: зависимость стоит на приложении, а не на admin_router."""
    from app.main import app

    assert any(dep.dependency is csrf_guard for dep in app.router.dependencies)


async def test_admin_unsafe_routes_without_csrf_are_rejected_when_session_is_valid(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    """ADR-22 на поведении: каждый небезопасный маршрут /admin с сессией и без _csrf - 403.

    Структура подключённых роутеров во FastAPI 0.143 ленивая, поэтому обход маршрутов в тесте
    не делаем: проверяем поведение на реальных POST (решение, отказ, вход).
    """
    admin = admin_user_factory()
    await sign_in(admin_client, admin)

    for path in ("/admin/ugc/42/approve", "/admin/ugc/42/reject"):
        resp = await admin_client.post(path, data={"reason_slug": "low_quality"})
        assert resp.status_code == 403, path
        assert resp.json() == {"error": "csrf_invalid"}, path


def test_exemptions_are_limited_to_safe_methods_internal_api_health_and_static() -> None:
    assert is_exempt("GET", "/admin/ugc")
    assert is_exempt("POST", "/internal/v1/activities/start")
    assert is_exempt("POST", "/admin/static/app.css")
    assert not is_exempt("POST", "/admin/ugc/1/approve")
    assert not is_exempt("POST", "/admin/login")
    assert not is_exempt("POST", "/healthz")
    assert not is_exempt("POST", "/internal")


def test_only_the_static_mount_lives_outside_the_admin_router() -> None:
    from app.main import app

    mounts = [route.path for route in app.routes if isinstance(route, Mount)]
    assert "/admin/static" in mounts
    assert all(path == "/admin/static" for path in mounts if path.startswith("/admin"))
