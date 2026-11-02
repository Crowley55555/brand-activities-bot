"""Фабрики фазы 4 (booking): слоты, записи, строки для проверок и вызовы core-дверей.

Slug'и активностей и брендов берутся из сида 0012 (правило 5). Слоты в сид не входят
(решение координатора, п.6): тесты создают их сами через `make_slot`. Все даты фиксированы:
NOW - 2026-10-12 08:00 по Москве, слоты «сегодня» - 12:00 и 13:00 по Москве.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.activities.booking import BookResult, book_slot, cancel_by_user, mark_arrived
from app.core.jobs.types import JobRecord
from app.core.types.ids import (
    AdminId,
    AppointmentId,
    ChannelAccountId,
    JobId,
    SlotId,
    UserId,
)
from app.core.types.ids import BrandId as _BrandId
from tests.factories import Guest

BRAND_A_SLUG = "brand_a_diagnostics_booking"
BRAND_C_SLUG = "brand_c_stylist_booking"
NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)  # 08:00 МСК
SLOT_NOON = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)  # 12:00 МСК
SLOT_ONE_PM = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)  # 13:00 МСК
REMIND_BEFORE = timedelta(minutes=5)
AUTO_AFTER = timedelta(minutes=5)


def clean_booking(engine: Engine) -> None:
    """Слоты и записи (appointments уходят по CASCADE). Прохождения и гости - в clean_db."""
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE slots RESTART IDENTITY CASCADE"))


def activity_id(engine: Engine, slug: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT id FROM activities WHERE slug = :s"), {"s": slug}
            ).scalar_one()
        )


def brand_id(engine: Engine, slug: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text("SELECT id FROM brands WHERE slug = :s"), {"s": slug}).scalar_one()
        )


def make_slot(
    engine: Engine,
    slug: str,
    start_at: datetime,
    *,
    duration: int = 15,
    kind: str = "bookable",
) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO slots (activity_id, start_at, duration_minutes, kind, "
                "created_at, updated_at) "
                "SELECT a.id, :ts, :d, :k, now(), now() FROM activities a WHERE a.slug = :s "
                "RETURNING id"
            ),
            {"ts": start_at, "d": duration, "k": kind, "s": slug},
        ).scalar_one()
    return int(row)


def appointment_row(engine: Engine, appointment_id: int) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT id, attempt_id, slot_id, status, remind_at, auto_cancel_at, "
                "reminded_at, arrived_at, arrived_marked_by_admin_id, cancelled_at "
                "FROM appointments WHERE id = :id"
            ),
            {"id": appointment_id},
        ).one()


def appointments_of_slot(engine: Engine, slot_id: int) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT id, status FROM appointments WHERE slot_id = :s ORDER BY id"),
                {"s": slot_id},
            ).all()
        )


def attempt_row(engine: Engine, attempt_id: int) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT id, user_id, activity_id, status, attempt_date, expires_at "
                "FROM attempts WHERE id = :id"
            ),
            {"id": attempt_id},
        ).one()


def attempts_of_user(engine: Engine, user_id: int) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT id, status FROM attempts WHERE user_id = :u ORDER BY id"),
                {"u": user_id},
            ).all()
        )


def jobs_by_kind(engine: Engine, kind: str) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT id, kind, status, payload, run_after, idempotency_key, last_error "
                    "FROM jobs WHERE kind = :k ORDER BY id"
                ),
                {"k": kind},
            ).all()
        )


def job_record(engine: Engine, job_id: int) -> JobRecord:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, kind, payload, status, attempts, max_attempts, "
                "idempotency_key, last_error FROM jobs WHERE id = :id"
            ),
            {"id": job_id},
        ).one()
    return JobRecord(
        id=JobId(int(row.id)),
        kind=str(row.kind),
        payload=dict(row.payload),
        status=str(row.status),
        attempts=int(row.attempts),
        max_attempts=int(row.max_attempts),
        idempotency_key=row.idempotency_key,
        last_error=row.last_error,
    )


def audit_rows(engine: Engine, action: str) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT actor_admin_id, actor_user_id, action, object_type, object_id "
                    "FROM audit_log WHERE action = :a ORDER BY id"
                ),
                {"a": action},
            ).all()
        )


def create_booking_admin(engine: Engine) -> int:
    """Сотрудник без прав на бренды; права выдаёт `grant_brand`."""
    login = f"sched-{uuid.uuid4().hex[:12]}"
    with engine.begin() as conn:
        admin_id = conn.execute(
            text(
                "INSERT INTO admin_users (login, password_hash, display_name) "
                "VALUES (:login, 'test-hash', 'Стенд') RETURNING id"
            ),
            {"login": login},
        ).scalar_one()
    return int(admin_id)


def grant_brand(engine: Engine, admin_id: int, brand_slug: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO admin_brands (admin_id, brand_id) "
                "SELECT :a, b.id FROM brands b WHERE b.slug = :s"
            ),
            {"a": admin_id, "s": brand_slug},
        )


async def book_as(
    uow_factory: Any, guest: Guest, slug: str, slot_id: int, now: datetime = NOW
) -> BookResult:
    async with uow_factory() as uow:
        return await book_slot(
            uow.repositories,
            UserId(guest.user_id),
            slug,
            ChannelAccountId(guest.channel_account_id),
            SlotId(slot_id),
            now,
        )


async def cancel_as(
    uow_factory: Any, guest: Guest, appointment_id: int, now: datetime = NOW
) -> Any:
    async with uow_factory() as uow:
        return await cancel_by_user(
            uow.repositories, AppointmentId(appointment_id), UserId(guest.user_id), now
        )


async def arrive_as(
    uow_factory: Any,
    appointment_id: int,
    admin_id: int,
    brand_ids: frozenset[int],
    now: datetime = NOW,
) -> None:
    async with uow_factory() as uow:
        await mark_arrived(
            uow.repositories,
            AppointmentId(appointment_id),
            AdminId(admin_id),
            frozenset(_BrandId(b) for b in brand_ids),
            now,
        )
