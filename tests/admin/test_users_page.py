"""Фаза 7a - /admin/users: маскирование ПДн, показ телефона с аудитом, CSRF, анонимизация."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.admin.conftest import CSRF_COOKIE, CreatedAdmin, sign_in
from tests.factories.booking import audit_rows

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
PHONE = "+79161234567"
EMAIL = "artem@example.com"


@pytest.fixture(autouse=True)
def _users_clean(sync_engine: Engine, clean_db: None) -> Any:
    with sync_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE audit_log RESTART IDENTITY CASCADE"))
    yield


def _insert_user(
    engine: Engine, *, phone: str | None, email: str | None, anonymized: bool = False
) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO users (first_name, last_name, phone, email, registered_at, "
                    "anonymized_at, created_at, updated_at) "
                    "VALUES (:fn, :ln, :ph, :em, :ts, :an, :ts, :ts) RETURNING id"
                ),
                {
                    "fn": None if anonymized else "Артём",
                    "ln": None if anonymized else "Петров",
                    "ph": phone,
                    "em": email,
                    "ts": NOW,
                    "an": NOW if anonymized else None,
                },
            ).scalar_one()
        )


async def _signed_in(
    client: AsyncClient, factory: Callable[..., CreatedAdmin]
) -> tuple[CreatedAdmin, str]:
    admin = factory(login="users-admin@test")
    await sign_in(client, admin)
    return admin, client.cookies.get(CSRF_COOKIE) or ""


async def test_users_list_without_session_redirects_to_login(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/admin/users")
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/admin/login")


async def test_users_list_shows_masked_pii(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    _insert_user(sync_engine, phone=PHONE, email=EMAIL)
    await _signed_in(admin_client, admin_user_factory)

    resp = await admin_client.get("/admin/users")

    assert resp.status_code == 200, resp.text
    assert PHONE not in resp.text
    assert EMAIL not in resp.text
    assert "Артём" not in resp.text
    assert "+79***4567" in resp.text
    assert "a***@e***.com" in resp.text


async def test_reveal_phone_without_csrf_rejected(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    uid = _insert_user(sync_engine, phone=PHONE, email=EMAIL)
    await _signed_in(admin_client, admin_user_factory)

    resp = await admin_client.post(f"/admin/users/{uid}/reveal_phone", data={})

    assert resp.status_code == 403
    assert PHONE not in resp.text
    assert audit_rows(sync_engine, "reveal_user_phone") == []


async def test_reveal_phone_writes_audit_log(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    uid = _insert_user(sync_engine, phone=PHONE, email=EMAIL)
    other = _insert_user(sync_engine, phone="+79990001122", email="b@c.ru")
    admin, csrf = await _signed_in(admin_client, admin_user_factory)

    resp = await admin_client.post(
        f"/admin/users/{uid}/reveal_phone", data={"_csrf": csrf, "page": "1"}
    )

    assert resp.status_code == 200, resp.text
    assert PHONE in resp.text  # раскрыт только этот номер
    assert "+79990001122" not in resp.text
    assert "+79***1122" in resp.text and other
    assert "location" not in resp.headers
    logged = audit_rows(sync_engine, "reveal_user_phone")
    assert len(logged) == 1
    assert logged[0].actor_admin_id == admin.id
    assert logged[0].object_type == "users"
    assert logged[0].object_id == uid
    with sync_engine.connect() as conn:
        details = conn.execute(text("SELECT details FROM audit_log")).scalar_one()
    assert details is None
    # Прямого показа без POST нет: GET с любыми параметрами номер не раскрывает.
    again = await admin_client.get(f"/admin/users?revealed={uid}")
    assert PHONE not in again.text


async def test_reveal_phone_for_anonymized_user_rejected(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    uid = _insert_user(sync_engine, phone=PHONE, email=EMAIL, anonymized=True)
    _, csrf = await _signed_in(admin_client, admin_user_factory)

    resp = await admin_client.post(
        f"/admin/users/{uid}/reveal_phone", data={"_csrf": csrf, "page": "1"}
    )

    assert resp.status_code == 409
    assert PHONE not in resp.text
    assert audit_rows(sync_engine, "reveal_user_phone") == []


async def test_users_list_shows_anonymized_label_without_pii(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    _insert_user(sync_engine, phone=None, email=None, anonymized=True)
    await _signed_in(admin_client, admin_user_factory)

    resp = await admin_client.get("/admin/users")

    assert resp.status_code == 200
    assert "Удалён" in resp.text
    assert "Показать телефон" not in resp.text
