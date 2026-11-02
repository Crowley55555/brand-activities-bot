"""Handler'ы фазы 4 (ADR-34) и сканеры (решение п.1 C): T7-T10 + too_late, has_reminders и
связка «джоб + сканер шлют одно напоминание».

Время задаётся часами ManualClock в deps.clock. Claim джоб идёт по now() БД, поэтому handler
вызывается напрямую с JobRecord из таблицы jobs (см. tests/factories/booking.job_record).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from app.core.activities.booking import run_due_scans
from app.core.jobs.types import JobRecord, TerminalBusinessError
from app.core.types.ids import JobId
from app.workers.context import WorkerDeps
from app.workers.handlers import auto_cancel_appointment, notify_appointment_reminder
from app.workers.scheduler_tick import scheduler_tick
from tests.conftest import ManualClock
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
    arrive_as,
    attempt_row,
    book_as,
    brand_id,
    cancel_as,
    clean_booking,
    create_booking_admin,
    grant_brand,
    job_record,
    jobs_by_kind,
    make_slot,
)


@pytest.fixture(autouse=True)
def _booking_clean(sync_engine: Engine) -> Any:
    clean_booking(sync_engine)
    yield


class _Unused:
    """Порты файлов, S3 и sniffer: в booking-обработчиках не вызываются."""

    async def fetch(self, tg_file_id: str) -> tuple[bytes, str]:  # pragma: no cover
        raise AssertionError("Telegram не нужен в booking-обработчиках")

    async def put(self, bucket: str, key: str, data: bytes, content_type: str) -> str:
        raise AssertionError("S3 не нужен в booking-обработчиках")

    async def get(self, bucket: str, key: str) -> bytes:  # pragma: no cover
        raise AssertionError("S3 не нужен в booking-обработчиках")

    async def delete(self, bucket: str, key: str) -> None:  # pragma: no cover
        raise AssertionError("S3 не нужен в booking-обработчиках")

    def presigned_get(self, bucket: str, key: str, ttl_seconds: int) -> str:  # pragma: no cover
        raise AssertionError("S3 не нужен в booking-обработчиках")

    def sniff(self, data: bytes) -> Any:  # pragma: no cover
        raise AssertionError("sniff не нужен в booking-обработчиках")


def _deps(at: datetime) -> WorkerDeps:
    unused = _Unused()
    return WorkerDeps(
        files=unused,  # type: ignore[arg-type]
        storage=unused,  # type: ignore[arg-type]
        sniffer=unused,  # type: ignore[arg-type]
        clock=ManualClock(at),
        ugc_bucket="ugc-test",
    )


def _job_for(engine: Engine, kind: str, appointment_id: int) -> JobRecord:
    for row in jobs_by_kind(engine, kind):
        if row.payload.get("appointment_id") == appointment_id:
            return job_record(engine, int(row.id))
    raise AssertionError(f"нет джоба {kind} для записи {appointment_id}")


def _sends(engine: Engine, template: str) -> list[Any]:
    return [
        j for j in jobs_by_kind(engine, "telegram_send") if j.payload.get("template") == template
    ]


async def test_notify_reminder_runs_at_remind_at(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T7: в remind_at напоминание - reminded + telegram_send(appointment_reminder), ids only."""
    guest = create_guest(sync_engine, external_id="3001", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    booked = await book_as(uow_factory, guest, BRAND_C_SLUG, slot_id)
    remind_at = SLOT_ONE_PM - REMIND_BEFORE
    job = _job_for(sync_engine, "notify_appointment_reminder", int(booked.appointment_id))

    async with uow_factory() as uow:
        await notify_appointment_reminder.run(job, uow, _deps(remind_at))

    row = appointment_row(sync_engine, int(booked.appointment_id))
    assert row.status == "reminded"
    assert row.reminded_at == remind_at
    sends = _sends(sync_engine, "appointment_reminder")
    assert len(sends) == 1
    assert sends[0].payload == {
        "chat_id": int(guest.external_id),
        "template": "appointment_reminder",
        "context": {"appointment_id": int(booked.appointment_id)},
    }


async def test_reminder_terminal_if_already_cancelled(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T8: гость отменил до напоминания - handler терминален с not_booked, без отправки."""
    guest = create_guest(sync_engine, external_id="3002", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    booked = await book_as(uow_factory, guest, BRAND_C_SLUG, slot_id)
    await cancel_as(uow_factory, guest, int(booked.appointment_id))
    job = _job_for(sync_engine, "notify_appointment_reminder", int(booked.appointment_id))

    with pytest.raises(TerminalBusinessError) as info:
        async with uow_factory() as uow:
            await notify_appointment_reminder.run(job, uow, _deps(SLOT_ONE_PM - REMIND_BEFORE))

    assert info.value.slug == "not_booked"
    assert _sends(sync_engine, "appointment_reminder") == []


async def test_auto_cancel_runs_at_auto_cancel_at(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T9: в auto_cancel_at гость не пришёл - cancelled_auto, попытка cancelled, сообщение."""
    guest = create_guest(sync_engine, external_id="3003", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    booked = await book_as(uow_factory, guest, BRAND_C_SLUG, slot_id)
    auto_at = SLOT_ONE_PM + AUTO_AFTER
    job = _job_for(sync_engine, "auto_cancel_appointment", int(booked.appointment_id))

    async with uow_factory() as uow:
        await auto_cancel_appointment.run(job, uow, _deps(auto_at))

    row = appointment_row(sync_engine, int(booked.appointment_id))
    assert row.status == "cancelled_auto"
    assert row.cancelled_at == auto_at
    assert attempt_row(sync_engine, row.attempt_id).status == "cancelled"
    sends = _sends(sync_engine, "appointment_auto_cancelled")
    assert len(sends) == 1
    assert sends[0].payload["context"] == {"appointment_id": int(booked.appointment_id)}


async def test_auto_cancel_skipped_if_arrived(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T10: гость пришёл (arrived) - авто-отмена терминальна с already_resolved."""
    guest = create_guest(sync_engine, external_id="3004", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    booked = await book_as(uow_factory, guest, BRAND_C_SLUG, slot_id)
    admin_id = create_booking_admin(sync_engine)
    grant_brand(sync_engine, admin_id, "brand_c")
    await arrive_as(
        uow_factory,
        int(booked.appointment_id),
        admin_id,
        frozenset({brand_id(sync_engine, "brand_c")}),
    )
    job = _job_for(sync_engine, "auto_cancel_appointment", int(booked.appointment_id))

    with pytest.raises(TerminalBusinessError) as info:
        async with uow_factory() as uow:
            await auto_cancel_appointment.run(job, uow, _deps(SLOT_ONE_PM + AUTO_AFTER))

    assert info.value.slug == "already_resolved"
    assert appointment_row(sync_engine, int(booked.appointment_id)).status == "arrived"


async def test_reminder_too_late_is_terminal_without_send(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Решение п.9: now > slot.start_at - напоминание не отправляем, терминал too_late."""
    guest = create_guest(sync_engine, external_id="3005", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    booked = await book_as(uow_factory, guest, BRAND_C_SLUG, slot_id)
    job = _job_for(sync_engine, "notify_appointment_reminder", int(booked.appointment_id))

    with pytest.raises(TerminalBusinessError) as info:
        async with uow_factory() as uow:
            await notify_appointment_reminder.run(
                job, uow, _deps(SLOT_ONE_PM + timedelta(minutes=1))
            )

    assert info.value.slug == "too_late"
    row = appointment_row(sync_engine, int(booked.appointment_id))
    assert row.status == "booked"
    assert row.reminded_at is None
    assert _sends(sync_engine, "appointment_reminder") == []


async def test_reminder_handler_respects_disabled_reminders(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Решение п.2: защита в обработчике - при has_reminders=false напоминание терминально."""
    guest = create_guest(sync_engine, external_id="3006", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_A_SLUG, SLOT_NOON)
    booked = await book_as(uow_factory, guest, BRAND_A_SLUG, slot_id)
    job = JobRecord(
        id=JobId(999_001),
        kind="notify_appointment_reminder",
        payload={"appointment_id": int(booked.appointment_id)},
        status="locked",
        attempts=1,
        max_attempts=6,
        idempotency_key=None,
        last_error=None,
    )

    with pytest.raises(TerminalBusinessError) as info:
        async with uow_factory() as uow:
            await notify_appointment_reminder.run(job, uow, _deps(SLOT_NOON - REMIND_BEFORE))

    assert info.value.slug == "reminders_disabled"
    assert appointment_row(sync_engine, int(booked.appointment_id)).status == "booked"


async def test_scanner_reminds_due_brand_c_only(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Решение п.1 + п.2: сканер напоминает Бренд C, Бренд A (has_reminders=false) - нет."""
    brand_c_guest = create_guest(sync_engine, external_id="3007", now=NOW)
    brand_a_guest = create_guest(sync_engine, external_id="3008", now=NOW)
    brand_c = await book_as(
        uow_factory,
        brand_c_guest,
        BRAND_C_SLUG,
        make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM),
    )
    # Слот Бренд A в то же время: remind_at наступил, слот ещё не начался, авто-отмены нет.
    brand_a = await book_as(
        uow_factory, brand_a_guest, BRAND_A_SLUG, make_slot(sync_engine, BRAND_A_SLUG, SLOT_ONE_PM)
    )
    now = SLOT_ONE_PM - REMIND_BEFORE

    async with uow_factory() as uow:
        report = await run_due_scans(uow.repositories, now, limit=100)

    assert report.reminded == 1
    assert appointment_row(sync_engine, int(brand_c.appointment_id)).status == "reminded"
    assert appointment_row(sync_engine, int(brand_a.appointment_id)).status == "booked"
    sends = _sends(sync_engine, "appointment_reminder")
    assert [s.payload["chat_id"] for s in sends] == [int(brand_c_guest.external_id)]


async def test_scanner_auto_cancels_due_and_skips_too_late_reminders(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Сканер авто-отмены: в auto_cancel_at отменяет; напоминание после старта слота не шлёт."""
    guest = create_guest(sync_engine, external_id="3009", now=NOW)
    booked = await book_as(
        uow_factory, guest, BRAND_C_SLUG, make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    )

    async with uow_factory() as uow:
        report = await run_due_scans(uow.repositories, SLOT_ONE_PM + AUTO_AFTER, limit=100)

    assert report.reminded == 0
    assert report.auto_cancelled == 1
    row = appointment_row(sync_engine, int(booked.appointment_id))
    assert row.status == "cancelled_auto"
    assert len(_sends(sync_engine, "appointment_auto_cancelled")) == 1
    assert _sends(sync_engine, "appointment_reminder") == []


async def test_job_and_scanner_send_single_reminder(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Решение п.1 C: джоб напоминания сработал первым, сканер в тот же момент не шлёт второе."""
    guest = create_guest(sync_engine, external_id="3010", now=NOW)
    booked = await book_as(
        uow_factory, guest, BRAND_C_SLUG, make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    )
    remind_at = SLOT_ONE_PM - REMIND_BEFORE
    job = _job_for(sync_engine, "notify_appointment_reminder", int(booked.appointment_id))
    async with uow_factory() as uow:
        await notify_appointment_reminder.run(job, uow, _deps(remind_at))

    async with uow_factory() as uow:
        report = await run_due_scans(uow.repositories, remind_at, limit=100)

    assert report.reminded == 0
    assert len(_sends(sync_engine, "appointment_reminder")) == 1


async def test_scheduler_tick_runs_due_scans(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Решение п.1 C: scheduler_tick зовёт сканеры - запись в remind_at становится reminded."""
    guest = create_guest(sync_engine, external_id="3011", now=NOW)
    booked = await book_as(
        uow_factory, guest, BRAND_C_SLUG, make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    )
    remind_at = SLOT_ONE_PM - REMIND_BEFORE

    await scheduler_tick(uow_factory, remind_at, _deps(remind_at))

    assert appointment_row(sync_engine, int(booked.appointment_id)).status == "reminded"
    assert len(_sends(sync_engine, "appointment_reminder")) == 1
