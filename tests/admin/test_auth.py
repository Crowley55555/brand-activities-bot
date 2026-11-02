"""Приёмочные тесты фазы 1.5 (T1-T5): auth-периметр админки, правило 10."""

from __future__ import annotations

from collections.abc import Callable

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.admin.conftest import (
    SESSION_COOKIE,
    CreatedAdmin,
    fetch_csrf,
    find_set_cookie,
    login_form,
    sign_in,
)


async def test_get_admin_ugc_without_cookie_redirects_to_login(
    api_client: AsyncClient,
) -> None:
    """T1."""
    resp = await api_client.get("/admin/ugc")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/login?next=/admin/ugc"


async def test_post_login_wrong_password_no_cookie(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    """T2."""
    admin = admin_user_factory(login="admin@test")
    csrf = await fetch_csrf(admin_client)

    resp = await admin_client.post(
        "/admin/login", data=login_form(admin.login, "wrong", csrf)
    )

    assert resp.status_code in (200, 401)
    assert "неверный логин или пароль" in resp.text.lower()
    assert find_set_cookie(resp, SESSION_COOKIE) is None
    assert SESSION_COOKIE not in admin_client.cookies


async def test_post_login_correct_password_sets_session_cookie(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    """T3."""
    admin = admin_user_factory(login="admin@test")
    csrf = await fetch_csrf(admin_client)

    resp = await admin_client.post(
        "/admin/login", data=login_form(admin.login, admin.plain, csrf)
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/"
    cookie = find_set_cookie(resp, SESSION_COOKIE)
    assert cookie is not None
    assert cookie.value, "подписанное значение сессии не должно быть пустым"
    assert cookie.has("httponly")
    assert cookie.attrs["samesite"].lower() == "lax"
    assert cookie.attrs["path"] == "/admin"


async def test_post_admin_action_without_csrf_rejects_403(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    """T4 (фаза 1.5, после фазы 3b - на реальном маршруте): сессия есть, _csrf в форме нет.

    Заглушку placeholder-action удалили; проверку даёт app-level csrf_guard (ADR-22) на
    настоящем POST модерации. Зависимости выполняются только для найденного маршрута.
    """
    admin = admin_user_factory()
    await sign_in(admin_client, admin)

    resp = await admin_client.post("/admin/ugc/42/approve", data={"x": "1"})

    assert resp.status_code == 403
    assert resp.json() == {"error": "csrf_invalid"}


async def test_inactive_admin_cannot_reuse_valid_cookie(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    """T5: подпись cookie валидна, но админ деактивирован в БД."""
    admin = admin_user_factory()
    await sign_in(admin_client, admin)
    assert (await admin_client.get("/admin/")).status_code == 200

    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE admin_users SET is_active = FALSE WHERE id = :admin_id"),
            {"admin_id": admin.id},
        )

    resp = await admin_client.get("/admin/")

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/admin/login")
    cleared = find_set_cookie(resp, SESSION_COOKIE)
    assert cleared is not None
    assert cleared.attrs["max-age"] == "0"
    assert cleared.value.strip('"') == ""
