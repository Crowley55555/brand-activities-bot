"""Приёмочные тесты фазы 4 - /admin/schedule (ADR-38): T14 + RBAC, CSRF-форма и создание слота.

Идём через настоящий Postgres и ASGI-клиент админки. Вход - sign_in (Argon2id, сессионная
cookie), POST - с CSRF double-submit (поле _csrf, значение cookie). Права на бренд - admin_brands.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.admin.conftest import CSRF_COOKIE, CreatedAdmin, sign_in
from tests.factories import create_guest
from tests.factories.booking import (
    BRAND_C_SLUG,
    NOW,
    SLOT_ONE_PM,
    activity_id,
    appointment_row,
    audit_rows,
    book_as,
    clean_booking,
    grant_brand,
    make_slot,
)


@pytest.fixture(autouse=True)
def _booking_clean(sync_engine: Engine) -> Any:
    clean_booking(sync_engine)
    yield


async def _booked_brand_c(
    uow_factory: Any, sync_engine: Engine, external_id: str
) -> tuple[int, int]:
    guest = create_guest(sync_engine, external_id=external_id, now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    booked = await book_as(uow_factory, guest, BRAND_C_SLUG, slot_id)
    return slot_id, int(booked.appointment_id)


async def test_mark_arrived_updates_status_and_logs(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    uow_factory: Any,
    clean_db: None,
    sync_engine: Engine,
) -> None:
    """T14: админ с правом на brand_c отмечает «пришёл»: arrived, кто отметил, журнал."""
    slot_id, appointment_id = await _booked_brand_c(uow_factory, sync_engine, "5001")
    admin = admin_user_factory(login="stand-brand_c@test")
    grant_brand(sync_engine, admin.id, "brand_c")
    await sign_in(admin_client, admin)
    csrf = admin_client.cookies.get(CSRF_COOKIE)

    resp = await admin_client.post(
        f"/admin/schedule/{slot_id}/mark_arrived", data={"_csrf": csrf or ""}
    )

    assert resp.status_code == 302, resp.text
    row = appointment_row(sync_engine, appointment_id)
    assert row.status == "arrived"
    assert row.arrived_marked_by_admin_id == admin.id
    logged = audit_rows(sync_engine, "mark_arrived")
    assert len(logged) == 1
    assert logged[0].actor_admin_id == admin.id
    assert logged[0].object_type == "appointments"  # CHECK audit_log.object_type - имя таблицы
    assert logged[0].object_id == appointment_id


async def test_mark_arrived_forbidden_for_other_brand(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    uow_factory: Any,
    clean_db: None,
    sync_engine: Engine,
) -> None:
    """RBAC: админ только с brand_a не отмечает запись brand_c - 403, статус не меняется."""
    slot_id, appointment_id = await _booked_brand_c(uow_factory, sync_engine, "5002")
    admin = admin_user_factory(login="stand-brand_a@test")
    grant_brand(sync_engine, admin.id, "brand_a")
    await sign_in(admin_client, admin)
    csrf = admin_client.cookies.get(CSRF_COOKIE)

    resp = await admin_client.post(
        f"/admin/schedule/{slot_id}/mark_arrived", data={"_csrf": csrf or ""}
    )

    assert resp.status_code == 403
    assert resp.json() == {"error": "brand_access_denied"}
    assert appointment_row(sync_engine, appointment_id).status == "booked"


async def test_mark_no_show_cancels_record(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    uow_factory: Any,
    clean_db: None,
    sync_engine: Engine,
) -> None:
    """ADR-38: no_show - cancelled_auto (переиспользуем статус), попытка cancelled."""
    slot_id, appointment_id = await _booked_brand_c(uow_factory, sync_engine, "5003")
    admin = admin_user_factory(login="stand-noshow@test")
    grant_brand(sync_engine, admin.id, "brand_c")
    await sign_in(admin_client, admin)
    csrf = admin_client.cookies.get(CSRF_COOKIE)

    resp = await admin_client.post(
        f"/admin/schedule/{slot_id}/mark_no_show", data={"_csrf": csrf or ""}
    )

    assert resp.status_code == 302, resp.text
    assert appointment_row(sync_engine, appointment_id).status == "cancelled_auto"


async def test_schedule_page_lists_today_slots(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    uow_factory: Any,
    clean_db: None,
    sync_engine: Engine,
) -> None:
    """GET /admin/schedule: слоты сегодня по Москве с названием активности из сида 0012."""
    await _booked_brand_c(uow_factory, sync_engine, "5004")
    admin = admin_user_factory(login="stand-list@test")
    grant_brand(sync_engine, admin.id, "brand_c")
    await sign_in(admin_client, admin)

    resp = await admin_client.get("/admin/schedule")

    assert resp.status_code == 200
    assert "13:00" in resp.text
    assert "Запись к стилисту" in resp.text


async def test_create_slot_inserts_bookable_slot(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    clean_db: None,
    sync_engine: Engine,
) -> None:
    """POST create_slot: одиночный слот на сегодня (15:00 МСК = 12:00 UTC), kind=bookable."""
    admin = admin_user_factory(login="stand-create@test")
    grant_brand(sync_engine, admin.id, "brand_c")
    await sign_in(admin_client, admin)
    csrf = admin_client.cookies.get(CSRF_COOKIE)

    resp = await admin_client.post(
        "/admin/schedule/create_slot",
        data={
            "_csrf": csrf or "",
            "activity_id": str(activity_id(sync_engine, BRAND_C_SLUG)),
            "start_at": "2026-10-12T15:00",
            "duration_minutes": "15",
        },
    )

    assert resp.status_code == 302, resp.text
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT start_at, duration_minutes, kind FROM slots WHERE activity_id = :a"),
            {"a": activity_id(sync_engine, BRAND_C_SLUG)},
        ).all()
    assert len(rows) == 1
    assert rows[0].start_at == datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    assert rows[0].duration_minutes == 15
    assert rows[0].kind == "bookable"


async def test_create_slot_in_past_is_rejected(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    clean_db: None,
    sync_engine: Engine,
) -> None:
    """Слот в прошлом (07:00 МСК при NOW 08:00) - 422 slot_in_past, слота нет."""
    admin = admin_user_factory(login="stand-past@test")
    grant_brand(sync_engine, admin.id, "brand_c")
    await sign_in(admin_client, admin)
    csrf = admin_client.cookies.get(CSRF_COOKIE)

    # Поле start_at - местное время Москвы (как в форме): 07:00 МСК при NOW = 08:00 МСК.
    resp = await admin_client.post(
        "/admin/schedule/create_slot",
        data={
            "_csrf": csrf or "",
            "activity_id": str(activity_id(sync_engine, BRAND_C_SLUG)),
            "start_at": "2026-10-12T07:00",
            "duration_minutes": "15",
        },
    )

    assert resp.status_code == 422
    assert resp.json() == {"error": "slot_in_past"}
