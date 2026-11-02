"""ADR-9: signed-cookie сессия админки и периметр правила 10 (дополняет T1, T3, T5)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from httpx import AsyncClient
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.admin.conftest import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    CreatedAdmin,
    fetch_csrf,
    find_set_cookie,
    login_form,
    sign_in,
)
from tests.conftest import TEST_ADMIN_KEY

OTHER_KEY = "another-test-signing-key-yyyyyyyyyyyyyyyy"


def _cookie_for(
    admin: CreatedAdmin, *, signing_key: str = TEST_ADMIN_KEY, issued: datetime | None = None
) -> str:
    from app.admin.session import sign_session
    from app.core.admin_access import SessionPayload
    from app.core.types.ids import AdminId

    payload = SessionPayload(AdminId(admin.id), admin.login, issued or datetime.now(UTC))
    return sign_session(payload, signing_key)


def _with_cookie(value: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE}={value}"}


def _naive_utc(delta: timedelta) -> datetime:
    return (datetime.now(UTC) - delta).replace(tzinfo=None)


def _assert_rejected_and_cleared(resp: Any) -> None:
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/admin/login")
    cleared = find_set_cookie(resp, SESSION_COOKIE)
    assert cleared is not None, "битую сессию надо стереть из браузера"
    assert cleared.attrs["max-age"] == "0"
    assert cleared.value == ""


# ---- выдача сессии ---------------------------------------------------------


async def test_session_cookie_attributes_and_payload(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    from app.admin.session import load_session

    admin = admin_user_factory()

    resp = await sign_in(admin_client, admin)

    cookie = find_set_cookie(resp, SESSION_COOKIE)
    assert cookie is not None
    assert cookie.attrs["max-age"] == "28800", "TTL 8 часов (ADR-9)"
    assert cookie.has("httponly")
    assert cookie.attrs["samesite"].lower() == "lax"
    assert cookie.attrs["path"] == "/admin"
    assert not cookie.has("secure"), "COOKIE_SECURE=false в тестовой среде"
    payload = load_session(cookie.value, TEST_ADMIN_KEY)
    assert payload is not None
    assert (int(payload.admin_id), payload.login) == (admin.id, admin.login)
    assert payload.issued_at == datetime(2026, 10, 12, 5, 0, tzinfo=UTC)  # ManualClock


async def test_session_and_csrf_cookies_get_secure_flag_when_enabled(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    override_settings: Callable[..., None],
) -> None:
    override_settings(COOKIE_SECURE="true")
    admin = admin_user_factory()

    page = await admin_client.get("/admin/login")
    csrf_cookie = find_set_cookie(page, CSRF_COOKIE)
    assert csrf_cookie is not None and csrf_cookie.has("secure")

    # Secure-cookie по http не вернётся из jar, поэтому отдаём её заголовком.
    resp = await admin_client.post(
        "/admin/login",
        data=login_form(admin.login, admin.plain, csrf_cookie.value),
        headers={"Cookie": f"{CSRF_COOKIE}={csrf_cookie.value}"},
    )

    assert resp.status_code == 302
    session = find_set_cookie(resp, SESSION_COOKIE)
    assert session is not None and session.has("secure")


async def test_login_updates_last_login_at(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    admin = admin_user_factory()
    with sync_engine.connect() as conn:
        before = conn.execute(
            text("SELECT last_login_at FROM admin_users WHERE id = :i"), {"i": admin.id}
        ).scalar_one()
    assert before is None

    await sign_in(admin_client, admin)

    with sync_engine.connect() as conn:
        after = conn.execute(
            text("SELECT last_login_at FROM admin_users WHERE id = :i"), {"i": admin.id}
        ).scalar_one()
    assert after == datetime(2026, 10, 12, 5, 0, tzinfo=UTC)


# ---- отказ во входе --------------------------------------------------------


async def test_unknown_login_and_wrong_password_are_indistinguishable(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    admin = admin_user_factory()
    csrf = await fetch_csrf(admin_client)

    wrong_pw = await admin_client.post(
        "/admin/login", data=login_form(admin.login, "wrong-one", csrf)
    )
    no_such = await admin_client.post(
        "/admin/login", data=login_form("nobody@test", "wrong-one", csrf)
    )

    assert wrong_pw.status_code == no_such.status_code == 200
    assert wrong_pw.text == no_such.text
    assert find_set_cookie(no_such, SESSION_COOKIE) is None


@pytest.mark.parametrize("state", [{"is_active": False}, {"deleted": True}])
async def test_inactive_or_deleted_admin_cannot_log_in_even_with_right_password(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    state: dict[str, bool],
) -> None:
    admin = admin_user_factory(**state)
    csrf = await fetch_csrf(admin_client)

    resp = await admin_client.post(
        "/admin/login", data=login_form(admin.login, admin.plain, csrf)
    )

    assert resp.status_code == 200
    assert "неверный логин или пароль" in resp.text
    assert find_set_cookie(resp, SESSION_COOKIE) is None


@pytest.mark.parametrize(
    "hostile_next",
    [
        "//evil.example/admin",
        "https://evil.example/",
        "/\\evil.example",
        "/administrator",
        "/admin@evil.example",
        "javascript:alert(1)",
        "/admin/login",
        "/admin/login/",
        "/admin/logout",
        "/admin/logout/?x=1",
        "/admin/\r\nSet-Cookie: x=1",
    ],
)
async def test_login_ignores_offsite_or_looping_next(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    hostile_next: str,
) -> None:
    admin = admin_user_factory()
    csrf = await fetch_csrf(admin_client)

    resp = await admin_client.post(
        "/admin/login", data=login_form(admin.login, admin.plain, csrf, hostile_next)
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/"


async def test_login_returns_to_safe_next(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    admin = admin_user_factory()
    csrf = await fetch_csrf(admin_client)

    resp = await admin_client.post(
        "/admin/login",
        data=login_form(admin.login, admin.plain, csrf, "/admin/ugc?status=pending"),
    )

    assert resp.headers["location"] == "/admin/ugc?status=pending"


async def test_login_form_reflects_only_safe_next_and_escapes_it(
    admin_client: AsyncClient,
) -> None:
    safe = await admin_client.get("/admin/login", params={"next": "/admin/ugc"})
    assert 'name="next" value="/admin/ugc"' in safe.text

    offsite = await admin_client.get("/admin/login", params={"next": "//evil.example"})
    assert 'name="next" value="/admin/"' in offsite.text

    xss = await admin_client.get(
        "/admin/login", params={"next": '/admin/"><script>alert(1)</script>'}
    )
    assert "<script>alert(1)" not in xss.text
    assert "&lt;script&gt;" in xss.text


# ---- проверка cookie на каждом запросе -------------------------------------


async def test_tampered_signature_is_rejected_and_cleared(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    valid = _cookie_for(admin_user_factory())
    # Портим предпоследний символ base64: последний несёт 4 бита, значения A..D
    # дают валидную подпись в 6% прогонов (bit-alignment base64url).
    flipped = valid[:-2] + ("A" if valid[-2] != "A" else "B") + valid[-1]

    resp = await admin_client.get("/admin/", headers=_with_cookie(flipped))

    _assert_rejected_and_cleared(resp)
    assert resp.headers["location"] == "/admin/login?next=/admin/"


@pytest.mark.parametrize("junk", ["garbage", "a.b.c", "x" * 3000])
async def test_garbage_cookie_is_rejected_and_cleared(
    admin_client: AsyncClient, junk: str
) -> None:
    resp = await admin_client.get("/admin/", headers=_with_cookie(junk))

    _assert_rejected_and_cleared(resp)


async def test_cookie_signed_with_another_key_is_rejected(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    forged = _cookie_for(admin_user_factory(), signing_key=OTHER_KEY)

    resp = await admin_client.get("/admin/", headers=_with_cookie(forged))

    _assert_rejected_and_cleared(resp)


async def test_expired_cookie_is_rejected_after_eight_hours(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    admin = admin_user_factory()
    with freeze_time(_naive_utc(timedelta(hours=8, minutes=5))):
        stale = _cookie_for(admin)

    resp = await admin_client.get("/admin/", headers=_with_cookie(stale))

    _assert_rejected_and_cleared(resp)


async def test_cookie_just_inside_ttl_is_accepted(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    admin = admin_user_factory()
    with freeze_time(_naive_utc(timedelta(hours=7, minutes=50))):
        fresh_enough = _cookie_for(admin)

    resp = await admin_client.get("/admin/", headers=_with_cookie(fresh_enough))

    assert resp.status_code == 200


async def test_cookie_of_soft_deleted_admin_is_rejected(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    admin = admin_user_factory(deleted=True)  # is_active=TRUE, но deleted_at задан

    resp = await admin_client.get("/admin/", headers=_with_cookie(_cookie_for(admin)))

    _assert_rejected_and_cleared(resp)
    assert resp.headers["location"] == "/admin/login", "у мёртвой сессии next не нужен"


async def test_cookie_for_nonexistent_admin_is_rejected(
    admin_client: AsyncClient,
) -> None:
    ghost = CreatedAdmin(id=987654, login="ghost@test", plain="irrelevant")

    resp = await admin_client.get("/admin/", headers=_with_cookie(_cookie_for(ghost)))

    _assert_rejected_and_cleared(resp)


async def test_logout_clears_cookie_and_drops_session_from_client(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    """I-3 фазы 10b-2: logout теперь POST с CSRF (канон 07 §4.4, правило 10)."""
    await sign_in(admin_client, admin_user_factory())
    assert (await admin_client.get("/admin/")).status_code == 200
    csrf = admin_client.cookies.get(CSRF_COOKIE)
    assert csrf is not None

    out = await admin_client.post("/admin/logout", data={"_csrf": csrf})

    assert out.status_code == 302
    assert out.headers["location"] == "/admin/login"
    cleared = find_set_cookie(out, SESSION_COOKIE)
    assert cleared is not None and cleared.attrs["max-age"] == "0"
    assert SESSION_COOKIE not in admin_client.cookies
    assert (await admin_client.get("/admin/")).status_code == 302


# ---- периметр: пути, методы, статика, конфигурация --------------------------


async def test_unknown_admin_paths_redirect_instead_of_revealing_404(
    api_client: AsyncClient,
) -> None:
    for path in ("/admin", "/admin/", "/admin/does-not-exist", "/admin/ugc/42/edit"):
        resp = await api_client.get(path)
        assert resp.status_code == 302, path
        assert resp.headers["location"].startswith("/admin/login?next=/admin"), path


async def test_next_keeps_query_string_percent_encoded(api_client: AsyncClient) -> None:
    resp = await api_client.get("/admin/ugc?status=pending&page=2")

    assert resp.headers["location"] == "/admin/login?next=/admin/ugc%3Fstatus%3Dpending%26page%3D2"


async def test_unsafe_methods_without_session_redirect_to_plain_login(
    api_client: AsyncClient,
) -> None:
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        resp = await api_client.request(method, "/admin/ugc/42/approve")
        assert resp.status_code == 302, method
        assert resp.headers["location"] == "/admin/login", method


async def _raw_get(path: str) -> int:
    """GET прямо в ASGI-приложение с «сырым» путём: httpx нормализовал бы `..`."""
    from app.main import app

    status: list[int] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    return status[0]


@pytest.mark.parametrize(
    "path",
    [
        "/admin/static/../ugc",
        "/admin/static/../templates/login.html",
        "//admin/ugc",
        "/admin/./ugc",
        "/admin/../admin/ugc",
        "/ADMIN/ugc",
    ],
)
async def test_path_tricks_neither_bypass_the_session_check_nor_escape_static(
    api_client: AsyncClient, path: str
) -> None:
    status = await _raw_get(path)

    assert status in (302, 404), (path, status)


async def test_static_assets_are_public(api_client: AsyncClient) -> None:
    css = await api_client.get("/admin/static/app.css")
    js = await api_client.get("/admin/static/htmx.min.js")

    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    assert js.status_code == 200


async def test_index_escapes_display_name_and_forbids_caching_and_framing(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
) -> None:
    admin = admin_user_factory(display_name='<b>Анна</b> & "кавычки"')
    await sign_in(admin_client, admin)

    resp = await admin_client.get("/admin/")

    assert resp.status_code == 200
    assert "<b>Анна</b>" not in resp.text
    # Jinja2 (ADR-21) экранирует кавычки как &#34; - значение то же, форма другая.
    assert "&lt;b&gt;Анна&lt;/b&gt; &amp; &#34;кавычки&#34;" in resp.text
    # I-3 фазы 10b-2: logout — форма POST с CSRF, а не GET-ссылка.
    assert 'action="/admin/logout"' in resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("bad_key", ["", "<ADMIN_SESSION_SECRET>"])
async def test_admin_is_closed_with_503_when_signing_key_is_not_configured(
    api_client: AsyncClient,
    override_settings: Callable[..., None],
    bad_key: str,
) -> None:
    override_settings(ADMIN_SESSION_SECRET=bad_key)

    for path in ("/admin/login", "/admin/", "/admin/logout"):
        resp = await api_client.get(path)
        assert resp.status_code == 503, path
        assert resp.json() == {"error": "admin_auth_not_configured"}
    assert (await api_client.get("/admin/static/app.css")).status_code == 200


# ---- подпись: юниты ---------------------------------------------------------


def _hmac_sha256_serializer(signing_key: str) -> URLSafeTimedSerializer:
    from app.admin.session import SESSION_SALT

    return URLSafeTimedSerializer(
        signing_key,
        salt=SESSION_SALT,
        signer_kwargs={"digest_method": hashlib.sha256, "key_derivation": "hmac"},
    )


def test_session_is_signed_with_hmac_sha256_and_not_with_itsdangerous_default() -> None:
    from app.admin.session import SESSION_SALT, load_session
    from app.core.admin_access import SessionPayload
    from app.core.types.ids import AdminId

    raw = {"admin_id": 7, "login": "a@test", "issued_at": "2026-10-12T05:00:00+00:00"}
    assert load_session(_hmac_sha256_serializer(TEST_ADMIN_KEY).dumps(raw), TEST_ADMIN_KEY) == (
        SessionPayload(AdminId(7), "a@test", datetime(2026, 10, 12, 5, 0, tzinfo=UTC))
    )
    # Тот же ключ и соль, но библиотечный дефолт (HMAC-SHA1): должен быть отвергнут.
    weak = URLSafeTimedSerializer(TEST_ADMIN_KEY, salt=SESSION_SALT).dumps(raw)
    assert load_session(weak, TEST_ADMIN_KEY) is None


@pytest.mark.parametrize(
    "raw",
    [
        ["not", "a", "dict"],
        {"login": "a", "issued_at": "2026-10-12T05:00:00+00:00"},
        {"admin_id": "7", "login": "a", "issued_at": "2026-10-12T05:00:00+00:00"},
        {"admin_id": True, "login": "a", "issued_at": "2026-10-12T05:00:00+00:00"},
        {"admin_id": 7, "login": 5, "issued_at": "2026-10-12T05:00:00+00:00"},
        {"admin_id": 7, "login": "a", "issued_at": "yesterday"},
        {"admin_id": 7, "login": "a"},
    ],
)
def test_validly_signed_but_malformed_payload_is_rejected(raw: Any) -> None:
    from app.admin.session import load_session

    signed = _hmac_sha256_serializer(TEST_ADMIN_KEY).dumps(raw)

    assert load_session(signed, TEST_ADMIN_KEY) is None


def test_unusable_signing_keys_cannot_sign_or_load() -> None:
    from app.admin.session import is_secret_usable, load_session, sign_session
    from app.core.admin_access import SessionPayload
    from app.core.types.ids import AdminId

    payload = SessionPayload(AdminId(1), "a", datetime(2026, 10, 12, tzinfo=UTC))
    for bad_key in ("", "<ADMIN_SESSION_SECRET>", "<X>"):
        assert is_secret_usable(bad_key) is False
        with pytest.raises(RuntimeError):
            sign_session(payload, bad_key)
        assert load_session("anything", bad_key) is None
    assert is_secret_usable(TEST_ADMIN_KEY) is True
