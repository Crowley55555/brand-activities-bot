"""Приёмочные тесты фазы 4 - ядро booking (ADR-33): T1-T6 + кейсы решений координатора.

Идём через настоящий Postgres (фикстуры tests/conftest.py) и core-двери, без моков.
Миграция 0012 создаёт две активности (brand_a, brand_c); слоты создаёт фабрика make_slot.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.common.errors import ConflictError, Forbidden
from app.core.activities.booking import BookResult, mark_no_show
from app.core.types.ids import AdminId, AppointmentId
from tests.factories import create_guest
from tests.factories.booking import (
    BRAND_C_SLUG,
    AUTO_AFTER,
    BRAND_A_SLUG,
    NOW,
    REMIND_BEFORE,
    SLOT_NOON,
    SLOT_ONE_PM,
    appointment_row,
    appointments_of_slot,
    arrive_as,
    attempt_row,
    attempts_of_user,
    audit_rows,
    book_as,
    brand_id,
    cancel_as,
    clean_booking,
    create_booking_admin,
    grant_brand,
    jobs_by_kind,
    make_slot,
)


@pytest.fixture(autouse=True)
def _booking_clean(sync_engine: Engine) -> Any:
    clean_booking(sync_engine)
    yield


async def test_book_without_consent_rejects(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T1: без действующего согласия book_slot отвечает consent_required и ничего не пишет."""
    guest = create_guest(sync_engine, external_id="2001", with_consent=False, now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)

    with pytest.raises(ConflictError) as info:
        await book_as(uow_factory, guest, BRAND_C_SLUG, slot_id)

    assert info.value.code == "consent_required"
    assert appointments_of_slot(sync_engine, slot_id) == []
    assert attempts_of_user(sync_engine, guest.user_id) == []
    assert jobs_by_kind(sync_engine, "auto_cancel_appointment") == []


async def test_book_happy_path(uow_factory: Any, clean_db: None, sync_engine: Engine) -> None:
    """T2: Бренд C - запись booked, два джоба: напоминание и авто-отмена с run_after."""
    guest = create_guest(sync_engine, external_id="2002", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)

    result = await book_as(uow_factory, guest, BRAND_C_SLUG, slot_id)

    assert isinstance(result, BookResult)
    assert result.has_reminders is True
    assert result.remind_before_minutes == 5
    row = appointment_row(sync_engine, int(result.appointment_id))
    assert row.status == "booked"
    assert row.remind_at == SLOT_ONE_PM - REMIND_BEFORE
    assert row.auto_cancel_at == SLOT_ONE_PM + AUTO_AFTER
    reminders = jobs_by_kind(sync_engine, "notify_appointment_reminder")
    cancels = jobs_by_kind(sync_engine, "auto_cancel_appointment")
    assert len(reminders) == 1 and len(cancels) == 1
    assert reminders[0].run_after == SLOT_ONE_PM - REMIND_BEFORE
    assert cancels[0].run_after == SLOT_ONE_PM + AUTO_AFTER
    assert reminders[0].idempotency_key == f"reminder:{int(result.appointment_id)}"
    assert cancels[0].idempotency_key == f"autocancel:{int(result.appointment_id)}"


async def test_book_brand_a_enqueues_only_auto_cancel(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T2 (Бренд A): has_reminders=false - напоминания нет, авто-отмена ставится всегда."""
    guest = create_guest(sync_engine, external_id="2005", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_A_SLUG, SLOT_NOON)

    result = await book_as(uow_factory, guest, BRAND_A_SLUG, slot_id)

    assert result.has_reminders is False
    assert jobs_by_kind(sync_engine, "notify_appointment_reminder") == []
    assert len(jobs_by_kind(sync_engine, "auto_cancel_appointment")) == 1


async def test_slot_race_second_booker_gets_409(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T3: два параллельных бронирования одного слота - одна запись, второй получает slot_taken."""
    first = create_guest(sync_engine, external_id="2003", now=NOW)
    second = create_guest(sync_engine, external_id="2004", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)

    results = await asyncio.gather(
        book_as(uow_factory, first, BRAND_C_SLUG, slot_id),
        book_as(uow_factory, second, BRAND_C_SLUG, slot_id),
        return_exceptions=True,
    )

    wins = [r for r in results if isinstance(r, BookResult)]
    losses = [r for r in results if isinstance(r, ConflictError)]
    assert len(wins) == 1
    assert len(losses) == 1 and losses[0].code == "slot_taken"
    assert len(appointments_of_slot(sync_engine, slot_id)) == 1


async def test_cancel_by_user_atomic(uow_factory: Any, clean_db: None, sync_engine: Engine) -> None:
    """T4: отмена гостем атомарна; джобы остаются (handler проверит статус); повтор - 409."""
    guest = create_guest(sync_engine, external_id="2006", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    booked = await book_as(uow_factory, guest, BRAND_C_SLUG, slot_id)

    cancelled = await cancel_as(uow_factory, guest, int(booked.appointment_id))

    assert cancelled.status == "cancelled_by_user"
    row = appointment_row(sync_engine, int(booked.appointment_id))
    assert row.status == "cancelled_by_user"
    assert row.cancelled_at is not None
    assert attempt_row(sync_engine, row.attempt_id).status == "cancelled"
    assert [j.status for j in jobs_by_kind(sync_engine, "notify_appointment_reminder")] == [
        "queued"
    ]
    assert [j.status for j in jobs_by_kind(sync_engine, "auto_cancel_appointment")] == ["queued"]
    with pytest.raises(ConflictError) as info:
        await cancel_as(uow_factory, guest, int(booked.appointment_id))
    assert info.value.code == "not_cancellable"


async def test_mark_arrived_requires_admin_brand(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T5: админ без прав на бренд получает 403 brand_access_denied, запись не меняется."""
    guest = create_guest(sync_engine, external_id="2007", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    booked = await book_as(uow_factory, guest, BRAND_C_SLUG, slot_id)
    admin_id = create_booking_admin(sync_engine)

    with pytest.raises(Forbidden) as info:
        await arrive_as(uow_factory, int(booked.appointment_id), admin_id, frozenset())

    assert info.value.code == "brand_access_denied"
    assert appointment_row(sync_engine, int(booked.appointment_id)).status == "booked"


async def test_already_booked_today_rejects_second(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T6: после отмены повторная запись той же активности сегодня - already_passed_today."""
    guest = create_guest(sync_engine, external_id="2008", now=NOW)
    first_slot = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    second_slot = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM + timedelta(minutes=30))
    booked = await book_as(uow_factory, guest, BRAND_C_SLUG, first_slot)
    await cancel_as(uow_factory, guest, int(booked.appointment_id))

    with pytest.raises(ConflictError) as info:
        await book_as(uow_factory, guest, BRAND_C_SLUG, second_slot)

    assert info.value.code == "already_passed_today"
    assert appointments_of_slot(sync_engine, second_slot) == []


async def test_already_booked_today_rejects_while_active(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T6b: пока первая запись активна, вторая в тот же день тоже отклоняется."""
    guest = create_guest(sync_engine, external_id="2009", now=NOW)
    first_slot = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    second_slot = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM + timedelta(minutes=30))
    await book_as(uow_factory, guest, BRAND_C_SLUG, first_slot)

    with pytest.raises(ConflictError) as info:
        await book_as(uow_factory, guest, BRAND_C_SLUG, second_slot)

    assert info.value.code == "already_passed_today"


async def test_book_past_slot_returns_slot_taken(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Решение п.8: слот, который уже начался (07:00 МСК при NOW 08:00), - slot_taken."""
    guest = create_guest(sync_engine, external_id="2010", now=NOW)
    past_slot = make_slot(sync_engine, BRAND_C_SLUG, NOW - timedelta(hours=1))

    with pytest.raises(ConflictError) as info:
        await book_as(uow_factory, guest, BRAND_C_SLUG, past_slot)

    assert info.value.code == "slot_taken"
    assert appointments_of_slot(sync_engine, past_slot) == []
    assert attempts_of_user(sync_engine, guest.user_id) == []


async def test_book_slot_of_other_activity_returns_slot_taken(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Слот Бренд A нельзя занять через активность Бренд C: slot_taken."""
    guest = create_guest(sync_engine, external_id="2011", now=NOW)
    brand_a_slot = make_slot(sync_engine, BRAND_A_SLUG, SLOT_NOON)

    with pytest.raises(ConflictError) as info:
        await book_as(uow_factory, guest, BRAND_C_SLUG, brand_a_slot)

    assert info.value.code == "slot_taken"


async def test_mark_arrived_completes_attempt_and_grants_leaflet(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Решения п.3/п.11: arrived -> attempt completed; награда leaflet в той же транзакции."""
    guest = create_guest(sync_engine, external_id="2012", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    booked = await book_as(uow_factory, guest, BRAND_C_SLUG, slot_id)
    admin_id = create_booking_admin(sync_engine)
    grant_brand(sync_engine, admin_id, "brand_c")
    brand_c = frozenset({brand_id(sync_engine, "brand_c")})

    await arrive_as(uow_factory, int(booked.appointment_id), admin_id, brand_c)

    row = appointment_row(sync_engine, int(booked.appointment_id))
    assert row.status == "arrived"
    assert row.arrived_at == NOW
    assert row.arrived_marked_by_admin_id == admin_id
    assert attempt_row(sync_engine, row.attempt_id).status == "completed"
    with sync_engine.connect() as conn:
        rewards = conn.execute(
            text("SELECT reward_type, title, code FROM rewards WHERE attempt_id = :a"),
            {"a": row.attempt_id},
        ).all()
    assert [(r.reward_type, r.title, r.code) for r in rewards] == [
        ("leaflet", "Листовка Бренд C", None)
    ]
    leaflet = [
        j
        for j in jobs_by_kind(sync_engine, "telegram_send")
        if j.payload["template"] == "appointment_leaflet"
    ]
    assert len(leaflet) == 1
    assert leaflet[0].payload["chat_id"] == int(guest.external_id)
    assert leaflet[0].payload["context"] == {"appointment_id": int(booked.appointment_id)}


async def test_mark_arrived_twice_is_rejected(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Второй клик «пришёл» - not_arrivable, награда не дублируется (duplicate_grant не нужен)."""
    guest = create_guest(sync_engine, external_id="2013", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    booked = await book_as(uow_factory, guest, BRAND_C_SLUG, slot_id)
    admin_id = create_booking_admin(sync_engine)
    grant_brand(sync_engine, admin_id, "brand_c")
    brand_c = frozenset({brand_id(sync_engine, "brand_c")})
    await arrive_as(uow_factory, int(booked.appointment_id), admin_id, brand_c)

    with pytest.raises(ConflictError) as info:
        await arrive_as(uow_factory, int(booked.appointment_id), admin_id, brand_c)

    assert info.value.code == "not_arrivable"


async def test_mark_no_show_cancels_and_closes_attempt(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Решение п.3/п.4: no_show -> cancelled_auto, attempt cancelled, гость получает сообщение."""
    guest = create_guest(sync_engine, external_id="2014", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    booked = await book_as(uow_factory, guest, BRAND_C_SLUG, slot_id)
    admin_id = create_booking_admin(sync_engine)
    grant_brand(sync_engine, admin_id, "brand_c")
    brand_c = frozenset({brand_id(sync_engine, "brand_c")})

    async with uow_factory() as uow:
        await mark_no_show(
            uow.repositories,
            AppointmentId(int(booked.appointment_id)),
            AdminId(admin_id),
            brand_c,
            NOW,
        )

    row = appointment_row(sync_engine, int(booked.appointment_id))
    assert row.status == "cancelled_auto"
    assert row.cancelled_at == NOW
    assert attempt_row(sync_engine, row.attempt_id).status == "cancelled"
    sends = [
        j
        for j in jobs_by_kind(sync_engine, "telegram_send")
        if j.payload["template"] == "appointment_auto_cancelled"
    ]
    assert len(sends) == 1
    assert sends[0].payload["chat_id"] == int(guest.external_id)
    assert audit_rows(sync_engine, "mark_no_show")[0].object_id == int(booked.appointment_id)
