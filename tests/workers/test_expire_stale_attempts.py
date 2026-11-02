"""C-2 (фаза 10a): джоб expire_stale_attempts переводит просроченные in_progress в expired.

Канон 03 §1 attempts: TTL в поле `expires_at` ставит `start_attempt` (`stale_ttl_minutes`,
по умолчанию 120). Канон 04 §1.3: раз в минуту `scheduler_tick` сканирует просроченные и
закрывает их как `expired` одной атомарной пачкой (SKIP LOCKED, пачка 500). Без этого джоба
забытое in_progress держит `attempts_once_per_day` и блокирует повторный старт.

Правило 2 (ПДн в логах): джоб логирует только `event` и `count`, user-данных нет.
Правило 3 (headless-ядро): логика в `app/core/attempts/jobs.py`, вызов — в
`app/workers/scheduler_tick.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.workers.context import WorkerDeps
from app.workers.scheduler_tick import scheduler_tick
from tests.conftest import ManualClock
from tests.factories import create_guest
from tests.factories.survey import UowFactory, start

NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
# Бренд A: survey-активность без модерации, подходит под кейсы in_progress -> expired.
SURVEY_SLUG = "brand_a_skin_survey"


def _deps() -> WorkerDeps:
    return WorkerDeps(
        files=None,  # type: ignore[arg-type]
        storage=None,  # type: ignore[arg-type]
        sniffer=None,  # type: ignore[arg-type]
        clock=ManualClock(NOW),
        ugc_bucket="ugc-test",
    )


def _insert_attempt(
    engine: Engine,
    user_id: int,
    channel_id: int,
    activity_slug: str,
    status: str,
    expires_at: datetime | None,
    started_at: datetime,
) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO attempts "
                "(user_id, activity_id, channel_account_id, status, started_at, "
                " attempt_date, expires_at, created_at, updated_at) "
                "SELECT :uid, a.id, :cid, :status, :ts, "
                " (CAST(:ts AS timestamptz) AT TIME ZONE 'Europe/Moscow')::date, "
                " :exp, :ts, :ts "
                "FROM activities a WHERE a.slug = :slug "
                "RETURNING id"
            ),
            {
                "uid": user_id,
                "cid": channel_id,
                "status": status,
                "ts": started_at,
                "exp": expires_at,
                "slug": activity_slug,
            },
        ).scalar_one()
    return int(row)


def _status(engine: Engine, attempt_id: int) -> tuple[str, datetime | None]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, finished_at FROM attempts WHERE id = :id"),
            {"id": attempt_id},
        ).one()
    return str(row.status), row.finished_at


async def test_expired_in_progress_moves_to_expired(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """in_progress с expires_at = NOW - 1 min уходит в expired с finished_at = NOW."""
    guest = create_guest(sync_engine, external_id="7101", now=NOW)
    attempt_id = _insert_attempt(
        sync_engine,
        guest.user_id,
        guest.channel_account_id,
        SURVEY_SLUG,
        "in_progress",
        NOW - timedelta(minutes=1),
        NOW - timedelta(hours=1),
    )

    await scheduler_tick(uow_factory, NOW, _deps())

    status, finished_at = _status(sync_engine, attempt_id)
    assert status == "expired"
    assert finished_at is not None


async def test_not_expired_in_progress_untouched(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """in_progress с expires_at = NOW + 10 min остаётся в прежнем статусе."""
    guest = create_guest(sync_engine, external_id="7102", now=NOW)
    attempt_id = _insert_attempt(
        sync_engine,
        guest.user_id,
        guest.channel_account_id,
        SURVEY_SLUG,
        "in_progress",
        NOW + timedelta(minutes=10),
        NOW,
    )

    await scheduler_tick(uow_factory, NOW, _deps())

    status, finished_at = _status(sync_engine, attempt_id)
    assert status == "in_progress"
    assert finished_at is None


async def test_null_expires_at_is_not_expired(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """in_progress без expires_at джоб не трогает (fail-closed без TTL)."""
    guest = create_guest(sync_engine, external_id="7103", now=NOW)
    attempt_id = _insert_attempt(
        sync_engine,
        guest.user_id,
        guest.channel_account_id,
        SURVEY_SLUG,
        "in_progress",
        None,
        NOW,
    )

    await scheduler_tick(uow_factory, NOW, _deps())

    status, _ = _status(sync_engine, attempt_id)
    assert status == "in_progress"


async def test_pending_moderation_not_touched_by_stale_job(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Прохождение в pending_moderation не трогает джоб stale (у него свой джоб)."""
    guest = create_guest(sync_engine, external_id="7104", now=NOW)
    attempt_id = _insert_attempt(
        sync_engine,
        guest.user_id,
        guest.channel_account_id,
        SURVEY_SLUG,
        "pending_moderation",
        NOW - timedelta(hours=5),
        NOW - timedelta(hours=5),
    )

    await scheduler_tick(uow_factory, NOW, _deps())

    status, _ = _status(sync_engine, attempt_id)
    assert status == "pending_moderation"


async def test_scheduler_tick_frees_the_day(
    uow_factory: UowFactory,
    clean_db: None,
    sync_engine: Engine,
    manual_clock: ManualClock,
) -> None:
    """fix-1 I-2: scheduler_tick → start_attempt того же дня проходит.

    Инвариант уже закрыт миграцией 0008 (expired вне partial unique
    attempts_once_per_day) и сущностно покрыт
    `tests/activities/test_brand_a_survey.py::test_expired_attempt_frees_the_day`,
    где статус выставляется ручным UPDATE. Этот тест фиксирует сквозной путь C-2:
    настоящий `scheduler_tick` закрывает in_progress, потом настоящий start_survey
    того же (guest, activity, date) возвращает новый attempt_id без 409.
    """
    guest = create_guest(sync_engine, external_id="7106", now=NOW)
    stale_id = _insert_attempt(
        sync_engine,
        guest.user_id,
        guest.channel_account_id,
        SURVEY_SLUG,
        "in_progress",
        NOW - timedelta(minutes=1),
        NOW - timedelta(hours=1),
    )

    await scheduler_tick(uow_factory, NOW, _deps())

    status, _ = _status(sync_engine, stale_id)
    assert status == "expired"

    manual_clock.set(NOW)
    fresh = await start(uow_factory, guest, manual_clock, SURVEY_SLUG)
    assert fresh.attempt_id != stale_id


async def test_batch_size_cap(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Пачка ограничена сверху: при >500 просроченных один тик обрабатывает не более 500."""
    guest = create_guest(sync_engine, external_id="7105", now=NOW)
    # Создадим 501 просроченную запись в разных активностях-днях — чтобы partial unique не ругался.
    # Разводим по attempt_date через started_at. 501 строка — чтобы батч точно упёрся.
    ids: list[int] = []
    with sync_engine.begin() as conn:
        for i in range(501):
            started = NOW - timedelta(days=i + 1)
            row = conn.execute(
                text(
                    "INSERT INTO attempts "
                    "(user_id, activity_id, channel_account_id, status, started_at, "
                    " attempt_date, expires_at, created_at, updated_at) "
                    "SELECT :uid, a.id, :cid, 'in_progress', :ts, "
                    " (CAST(:ts AS timestamptz) AT TIME ZONE 'Europe/Moscow')::date, "
                    " :exp, :ts, :ts "
                    "FROM activities a WHERE a.slug = :slug "
                    "RETURNING id"
                ),
                {
                    "uid": guest.user_id,
                    "cid": guest.channel_account_id,
                    "ts": started,
                    "exp": started + timedelta(minutes=1),
                    "slug": SURVEY_SLUG,
                },
            ).scalar_one()
            ids.append(int(row))

    await scheduler_tick(uow_factory, NOW, _deps())

    # Первый тик закрывает не более 500 строк; одна остаётся в in_progress до следующего тика.
    with sync_engine.connect() as conn:
        remaining = conn.execute(
            text(
                "SELECT COUNT(*) FROM attempts WHERE id = ANY(:ids) AND status = 'in_progress'"
            ),
            {"ids": ids},
        ).scalar_one()
    assert remaining == 1

    # Следующий тик забирает хвост.
    await scheduler_tick(uow_factory, NOW, _deps())
    with sync_engine.connect() as conn:
        remaining = conn.execute(
            text(
                "SELECT COUNT(*) FROM attempts WHERE id = ANY(:ids) AND status = 'in_progress'"
            ),
            {"ids": ids},
        ).scalar_one()
    assert remaining == 0
