"""Фаза 10b-3, N-1: EXPIRE_BATCH_SIZE параметризует scheduler-джобы.

Канон 03 §1 + правило 5: лимит пачки scheduler-джобов берётся из env, не хардкод.
Поставим EXPIRE_BATCH_SIZE=5 через monkeypatch, создадим 6 просроченных строк и
проверим, что первый тик обработал ровно 5 (пачка упёрлась).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.workers.context import WorkerDeps
from app.workers.scheduler_tick import scheduler_tick
from tests.conftest import ManualClock
from tests.factories import create_guest

NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
SURVEY_SLUG = "brand_a_skin_survey"
UGC_SLUG = "brand_b_photo_subscription"


def _deps() -> WorkerDeps:
    return WorkerDeps(
        files=None,  # type: ignore[arg-type]
        storage=None,  # type: ignore[arg-type]
        sniffer=None,  # type: ignore[arg-type]
        clock=ManualClock(NOW),
        ugc_bucket="ugc-test",
    )


def _settings_with_batch(size: int) -> Any:
    from app.config import Settings

    return Settings(EXPIRE_BATCH_SIZE=size)


@pytest.fixture
def small_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбрасывает lru_cache get_settings, подменяет на малый batch=5."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("EXPIRE_BATCH_SIZE", "5")
    yield
    get_settings.cache_clear()
    monkeypatch.delenv("EXPIRE_BATCH_SIZE", raising=False)


async def test_expire_stale_attempts_respects_batch_size(
    small_batch: None,
    uow_factory: Any,
    clean_db: None,
    sync_engine: Engine,
) -> None:
    """EXPIRE_BATCH_SIZE=5: при 6 просроченных первый тик закрывает 5, остаётся 1."""
    guest = create_guest(sync_engine, external_id="9201", now=NOW)
    ids: list[int] = []
    with sync_engine.begin() as conn:
        for i in range(6):
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

    with sync_engine.connect() as conn:
        remaining = conn.execute(
            text(
                "SELECT COUNT(*) FROM attempts "
                "WHERE id = ANY(:ids) AND status = 'in_progress'"
            ),
            {"ids": ids},
        ).scalar_one()
    assert remaining == 1  # 6 - 5 = 1

    await scheduler_tick(uow_factory, NOW, _deps())
    with sync_engine.connect() as conn:
        remaining = conn.execute(
            text(
                "SELECT COUNT(*) FROM attempts "
                "WHERE id = ANY(:ids) AND status = 'in_progress'"
            ),
            {"ids": ids},
        ).scalar_one()
    assert remaining == 0


async def test_expire_pending_moderation_respects_batch_size(
    small_batch: None,
    uow_factory: Any,
    clean_db: None,
    sync_engine: Engine,
) -> None:
    """EXPIRE_BATCH_SIZE=5: 6 pending_moderation за TTL — первый тик закрывает 5."""
    # 6 разных гостей на UGC: unique attempts_once_per_day не ругается.
    attempt_ids: list[int] = []
    for i in range(6):
        guest = create_guest(sync_engine, external_id=f"930{i}", now=NOW)
        with sync_engine.begin() as conn:
            row = conn.execute(
                text(
                    "INSERT INTO attempts "
                    "(user_id, activity_id, channel_account_id, status, started_at, "
                    " attempt_date, created_at, updated_at) "
                    "SELECT :uid, a.id, :cid, 'pending_moderation', :ts, "
                    " (CAST(:ts AS timestamptz) AT TIME ZONE 'Europe/Moscow')::date, "
                    " :ts, :upd "
                    "FROM activities a WHERE a.slug = :slug "
                    "RETURNING id"
                ),
                {
                    "uid": guest.user_id,
                    "cid": guest.channel_account_id,
                    "ts": NOW - timedelta(minutes=200),
                    "upd": NOW - timedelta(minutes=200),
                    "slug": UGC_SLUG,
                },
            ).scalar_one()
            attempt_ids.append(int(row))

    await scheduler_tick(uow_factory, NOW, _deps())
    with sync_engine.connect() as conn:
        remaining = conn.execute(
            text(
                "SELECT COUNT(*) FROM attempts "
                "WHERE id = ANY(:ids) AND status = 'pending_moderation'"
            ),
            {"ids": attempt_ids},
        ).scalar_one()
    assert remaining == 1

    await scheduler_tick(uow_factory, NOW, _deps())
    with sync_engine.connect() as conn:
        remaining = conn.execute(
            text(
                "SELECT COUNT(*) FROM attempts "
                "WHERE id = ANY(:ids) AND status = 'pending_moderation'"
            ),
            {"ids": attempt_ids},
        ).scalar_one()
    assert remaining == 0
