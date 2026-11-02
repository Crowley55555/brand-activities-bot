"""core.attempts: старт, «один раз в день», закрытие, гонки (08 §4.1, 03 §2 Q3)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.common.errors import ConflictError, NotFound, ValidationError
from app.core.attempts import close_attempt, start_attempt
from app.core.types.enums import ActivityKind
from app.core.types.ids import AttemptId, ChannelAccountId, UserId
from tests.conftest import ManualClock
from tests.factories import Guest, create_guest, revoke_consent
from tests.factories.survey import SURVEY_SLUG, UowFactory


def _rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


async def _start(
    uow_factory: UowFactory,
    guest: Guest,
    clock: ManualClock,
    slug: str = SURVEY_SLUG,
    expected_kind: ActivityKind | None = None,
) -> Any:
    async with uow_factory() as uow:
        r = uow.repositories
        return await start_attempt(
            r.consents,
            r.catalog,
            r.attempts,
            UserId(guest.user_id),
            slug,
            ChannelAccountId(guest.channel_account_id),
            clock.now(),
            expected_kind=expected_kind,
        )


async def _close(
    uow_factory: UowFactory,
    attempt_id: int,
    clock: ManualClock,
    status: Any = "completed",
    summary: str | None = "skin_type=combo",
) -> Any:
    async with uow_factory() as uow:
        return await close_attempt(
            uow.repositories.attempts, AttemptId(attempt_id), status, summary, clock.now()
        )


@pytest.fixture
def restore_activity_flags(sync_engine: Engine) -> Iterator[None]:
    yield
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE activities SET is_active = TRUE, deleted_at = NULL WHERE slug = :s"),
            {"s": SURVEY_SLUG},
        )


async def test_start_attempt_creates_in_progress_attempt(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    guest = create_guest(sync_engine)

    result = await _start(uow_factory, guest, manual_clock)

    assert result.status == "in_progress"
    assert result.kind == "survey"
    assert result.activity_slug == SURVEY_SLUG
    row = _rows(sync_engine, "SELECT * FROM attempts")[0]
    assert row.id == result.attempt_id
    assert row.user_id == guest.user_id
    assert row.channel_account_id == guest.channel_account_id
    assert row.status == "in_progress"
    assert row.started_at == manual_clock.now()
    assert row.finished_at is None
    assert str(row.attempt_date) == "2026-10-12"


async def test_start_attempt_consent_is_checked_before_anything_is_written(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    guest = create_guest(sync_engine)
    revoke_consent(sync_engine, guest)

    with pytest.raises(ConflictError) as exc:
        await _start(uow_factory, guest, manual_clock)

    assert exc.value.code == "consent_required"
    assert _rows(sync_engine, "SELECT id FROM attempts") == []


async def test_start_attempt_unknown_activity_is_not_found(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    guest = create_guest(sync_engine)

    with pytest.raises(NotFound) as exc:
        await _start(uow_factory, guest, manual_clock, slug="no_such_activity")

    assert exc.value.code == "activity_not_found"


@pytest.mark.parametrize(
    "disable_sql",
    [
        "UPDATE activities SET is_active = FALSE WHERE slug = :s",
        "UPDATE activities SET deleted_at = now() WHERE slug = :s",
    ],
)
async def test_start_attempt_inactive_or_deleted_activity_is_not_found(
    disable_sql: str,
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
    restore_activity_flags: None,
) -> None:
    guest = create_guest(sync_engine)
    with sync_engine.begin() as conn:
        conn.execute(text(disable_sql), {"s": SURVEY_SLUG})

    with pytest.raises(NotFound) as exc:
        await _start(uow_factory, guest, manual_clock)

    assert exc.value.code == "activity_not_found"
    assert _rows(sync_engine, "SELECT id FROM attempts") == []


async def test_start_attempt_rejects_unexpected_activity_kind(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    guest = create_guest(sync_engine)

    with pytest.raises(ValidationError) as exc:
        await _start(uow_factory, guest, manual_clock, expected_kind="booking")

    assert exc.value.code == "activity_kind_mismatch"
    assert _rows(sync_engine, "SELECT id FROM attempts") == []


async def test_attempt_date_follows_moscow_midnight(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    """20:59 UTC - ещё 23:59 по Москве; 21:00 UTC - уже следующий день (UTC+3)."""
    guest = create_guest(sync_engine)
    manual_clock.set(datetime(2026, 10, 12, 20, 59, tzinfo=UTC))
    await _start(uow_factory, guest, manual_clock)
    manual_clock.set(datetime(2026, 10, 12, 21, 0, tzinfo=UTC))
    # Новая дата по Москве - второй старт того же гостя разрешён, пока первый ещё жив.
    await _start(uow_factory, guest, manual_clock)

    dates = [
        str(r.attempt_date)
        for r in _rows(sync_engine, "SELECT attempt_date FROM attempts ORDER BY id")
    ]
    assert dates == ["2026-10-12", "2026-10-13"]


# ------------------------------------------------------------ close_attempt


async def test_close_attempt_moves_in_progress_to_terminal_status(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    guest = create_guest(sync_engine)
    started = await _start(uow_factory, guest, manual_clock)
    manual_clock.set(manual_clock.now() + timedelta(minutes=3))

    closed = await _close(uow_factory, started.attempt_id, manual_clock)

    assert closed.status == "completed"
    assert closed.summary == "skin_type=combo"
    assert closed.finished_at == manual_clock.now()
    assert closed.activity_slug == SURVEY_SLUG
    row = _rows(sync_engine, "SELECT status, summary, finished_at FROM attempts")[0]
    assert (row.status, row.summary) == ("completed", "skin_type=combo")


async def test_second_close_is_rejected_as_already_closed(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    guest = create_guest(sync_engine)
    started = await _start(uow_factory, guest, manual_clock)
    await _close(uow_factory, started.attempt_id, manual_clock)

    with pytest.raises(ConflictError) as exc:
        await _close(uow_factory, started.attempt_id, manual_clock, status="cancelled")

    assert exc.value.code == "already_closed"
    assert _rows(sync_engine, "SELECT status FROM attempts")[0].status == "completed"


async def test_close_unknown_attempt_is_not_found(
    uow_factory: UowFactory, manual_clock: ManualClock, clean_db: None
) -> None:
    with pytest.raises(NotFound) as exc:
        await _close(uow_factory, 999_999, manual_clock)

    assert exc.value.code == "attempt_not_found"


@pytest.mark.parametrize("status", ["in_progress", "pending_moderation"])
async def test_close_rejects_non_terminal_status(
    status: str, uow_factory: UowFactory, manual_clock: ManualClock, clean_db: None
) -> None:
    with pytest.raises(ValidationError) as exc:
        await _close(uow_factory, 1, manual_clock, status=status)

    assert exc.value.code == "status_not_terminal"


async def test_close_never_sets_finished_before_started(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    """Часы ушли назад между стартом и финишем: CHECK finished_at >= started_at не падает."""
    guest = create_guest(sync_engine)
    started = await _start(uow_factory, guest, manual_clock)
    started_at = manual_clock.now()
    manual_clock.set(started_at - timedelta(seconds=30))

    closed = await _close(uow_factory, started.attempt_id, manual_clock)

    assert closed.finished_at == started_at


async def test_expired_attempt_cannot_be_closed_again(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    guest = create_guest(sync_engine)
    started = await _start(uow_factory, guest, manual_clock)
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE attempts SET status = 'expired'"))

    with pytest.raises(ConflictError) as exc:
        await _close(uow_factory, started.attempt_id, manual_clock)

    assert exc.value.code == "already_closed"


# ------------------------------------------------------------------ гонки


async def test_concurrent_starts_only_one_attempt_is_created(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    """Критерий ревью 20: два параллельных start_attempt - один проходит, второй 409.

    Первая транзакция вставила строку, но ещё не закоммитилась; вторая упирается в
    partial unique attempts_once_per_day и ждёт. После commit первой вторая получает
    0 строк из `ON CONFLICT DO NOTHING` -> already_passed_today.
    """
    guest = create_guest(sync_engine)
    first_inserted = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        async with uow_factory() as uow:
            r = uow.repositories
            await start_attempt(
                r.consents,
                r.catalog,
                r.attempts,
                UserId(guest.user_id),
                SURVEY_SLUG,
                ChannelAccountId(guest.channel_account_id),
                manual_clock.now(),
            )
            first_inserted.set()
            await release_first.wait()  # держим транзакцию открытой

    async def second() -> None:
        await first_inserted.wait()
        await _start(uow_factory, guest, manual_clock)

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_inserted.wait()
    await asyncio.sleep(0.4)
    assert not second_task.done(), "вторая транзакция должна ждать блокировку, а не проскочить"

    release_first.set()
    await first_task
    with pytest.raises(ConflictError) as exc:
        await second_task

    assert exc.value.code == "already_passed_today"
    assert len(_rows(sync_engine, "SELECT id FROM attempts")) == 1


async def test_concurrent_close_only_one_wins(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    guest = create_guest(sync_engine)
    started = await _start(uow_factory, guest, manual_clock)
    first_closed = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        async with uow_factory() as uow:
            await close_attempt(
                uow.repositories.attempts,
                AttemptId(started.attempt_id),
                "completed",
                "first",
                manual_clock.now(),
            )
            first_closed.set()
            await release_first.wait()

    async def second() -> None:
        await first_closed.wait()
        await _close(uow_factory, started.attempt_id, manual_clock, summary="second")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_closed.wait()
    await asyncio.sleep(0.4)
    assert not second_task.done()

    release_first.set()
    await first_task
    with pytest.raises(ConflictError) as exc:
        await second_task

    assert exc.value.code == "already_closed"
    assert _rows(sync_engine, "SELECT summary FROM attempts")[0].summary == "first"


async def test_start_attempt_fills_expires_at(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    """03 §1: expires_at = started_at + stale_ttl_minutes (по умолчанию 120), для джоба."""
    guest = create_guest(sync_engine)

    await _start(uow_factory, guest, manual_clock)

    row = _rows(sync_engine, "SELECT started_at, expires_at FROM attempts")[0]
    assert row.started_at == manual_clock.now()
    assert row.expires_at == row.started_at + timedelta(minutes=120)
