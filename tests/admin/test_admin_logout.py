"""Фаза 10b-2 (I-3): POST /admin/logout вместо GET.

Канон 07 §4.4 / правило 10: любая state-changing операция админки — POST под CSRF.
`<img src=/admin/logout>` в чужом HTML больше не разлогинивает админа.
"""

from __future__ import annotations

from collections.abc import Callable

from httpx import AsyncClient

from tests.admin.conftest import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    CreatedAdmin,
    find_set_cookie,
    sign_in,
)

# ---------------------------------------------------------------- 16. GET /admin/logout → 405


async def test_get_logout_rejects_with_405(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    """GET больше не разлогинивает (I-3). 405 Method Not Allowed — явный маркер
    того, что обработчика нет; важно, чтобы cookie при этом НЕ очищалась."""
    admin = admin_user_factory()
    await sign_in(admin_client, admin)
    assert admin_client.cookies.get(SESSION_COOKIE) is not None

    resp = await admin_client.get("/admin/logout")
    assert resp.status_code == 405, resp.text
    # Cookie не трогается этим ответом.
    assert find_set_cookie(resp, SESSION_COOKIE) is None
    # Сессия ещё жива — следующий GET /admin/ отдаёт страницу, не редиректит.
    whoami = await admin_client.get("/admin/")
    assert whoami.status_code == 200


# ---------------------------------------------------------------- 17. POST без CSRF → 403


async def test_post_logout_without_csrf_403(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    admin = admin_user_factory()
    await sign_in(admin_client, admin)

    resp = await admin_client.post("/admin/logout", data={})
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"error": "csrf_invalid"}
    # Сессия жива.
    whoami = await admin_client.get("/admin/")
    assert whoami.status_code == 200


# ---------------------------------- 18. POST с CSRF → 302 + cookie очищена


async def test_post_logout_with_csrf_clears_cookie_302(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    admin = admin_user_factory()
    await sign_in(admin_client, admin)
    csrf = admin_client.cookies.get(CSRF_COOKIE)
    assert csrf is not None

    resp = await admin_client.post("/admin/logout", data={"_csrf": csrf})
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"] == "/admin/login"
    cleared = find_set_cookie(resp, SESSION_COOKIE)
    assert cleared is not None
    assert cleared.attrs["max-age"] == "0"
    assert cleared.value.strip('"') == ""


# ---------------------------------------------------------------- доп. nav содержит форму POST


async def test_nav_renders_post_logout_form(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    """Ссылка `<a href="/admin/logout">` заменена формой POST c _csrf в nav."""
    admin = admin_user_factory()
    await sign_in(admin_client, admin)

    resp = await admin_client.get("/admin/")
    assert resp.status_code == 200
    # Нет сырого GET-линка на logout.
    assert 'href="/admin/logout"' not in resp.text
    # Есть форма POST с CSRF.
    assert 'action="/admin/logout"' in resp.text
    assert 'method="post"' in resp.text
    assert 'name="_csrf"' in resp.text
