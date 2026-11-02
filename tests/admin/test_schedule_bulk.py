"""Фаза 7d: POST /admin/schedule/bulk_create_slots - сессия, CSRF, создание и flash."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.admin.conftest import CSRF_COOKIE, CreatedAdmin, sign_in
from tests.factories.booking import BRAND_C_SLUG, activity_id, clean_booking, grant_brand

BULK = "/admin/schedule/bulk_create_slots"


@pytest.fixture(autouse=True)
def _booking_clean(sync_engine: Engine) -> Any:
    clean_booking(sync_engine)
    yield


def _form(engine: Engine, csrf: str, **over: str) -> dict[str, str]:
    data = {
        "_csrf": csrf,
        "activity_id": str(activity_id(engine, BRAND_C_SLUG)),
        "day_start_local": "2026-10-12T10:00",
        "day_end_local": "2026-10-12T12:00",
        "step_minutes": "30",
    }
    data.update(over)
    return data


def _slot_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM slots")).scalar_one())


async def test_bulk_without_session_redirects_to_login(
    api_client: AsyncClient, sync_engine: Engine
) -> None:
    resp = await api_client.post(BULK, data=_form(sync_engine, ""))

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/admin/login")


async def test_bulk_without_csrf_rejected(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    clean_db: None,
    sync_engine: Engine,
) -> None:
    admin = admin_user_factory(login="bulk-nocsrf@test")
    grant_brand(sync_engine, admin.id, "brand_c")
    await sign_in(admin_client, admin)
    data = _form(sync_engine, "")
    del data["_csrf"]

    resp = await admin_client.post(BULK, data=data)

    assert resp.status_code == 403
    assert resp.json() == {"error": "csrf_invalid"}
    assert _slot_count(sync_engine) == 0


async def test_bulk_creates_and_redirects_with_count(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    clean_db: None,
    sync_engine: Engine,
) -> None:
    admin = admin_user_factory(login="bulk-ok@test")
    grant_brand(sync_engine, admin.id, "brand_c")
    await sign_in(admin_client, admin)
    csrf = admin_client.cookies.get(CSRF_COOKIE) or ""

    resp = await admin_client.post(BULK, data=_form(sync_engine, csrf))

    assert resp.status_code == 302, resp.text
    assert resp.headers["location"] == "/admin/schedule?created=4&conflicts=0"
    assert _slot_count(sync_engine) == 4
    page = await admin_client.get(resp.headers["location"])
    assert "Создано 4 слотов" in page.text


async def test_bulk_validation_error_redirects_with_error_and_inserts_nothing(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    clean_db: None,
    sync_engine: Engine,
) -> None:
    admin = admin_user_factory(login="bulk-bad@test")
    grant_brand(sync_engine, admin.id, "brand_c")
    await sign_in(admin_client, admin)
    csrf = admin_client.cookies.get(CSRF_COOKIE) or ""

    resp = await admin_client.post(BULK, data=_form(sync_engine, csrf, step_minutes="3"))

    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/schedule?error=step_out_of_range"
    assert _slot_count(sync_engine) == 0


async def test_bulk_forbidden_for_other_brand(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    clean_db: None,
    sync_engine: Engine,
) -> None:
    admin = admin_user_factory(login="bulk-rbac@test")
    grant_brand(sync_engine, admin.id, "brand_a")
    await sign_in(admin_client, admin)
    csrf = admin_client.cookies.get(CSRF_COOKIE) or ""

    resp = await admin_client.post(BULK, data=_form(sync_engine, csrf))

    assert resp.status_code == 403
    assert _slot_count(sync_engine) == 0
