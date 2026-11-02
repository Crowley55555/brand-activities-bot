"""M1 (ревью фазы 4): сбой сканера booking не откатывает разблокировку джобов.

Залипшая блокировка (locked, истекла) должна вернуться в queued даже тогда, когда сканер падает:
разблокировка и сканеры идут в разных транзакциях scheduler_tick.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

import app.workers.scheduler_tick as scheduler_tick_module
from app.workers.context import WorkerDeps
from app.workers.scheduler_tick import scheduler_tick
from tests.conftest import ManualClock
from tests.factories.booking import clean_booking

NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)


def _deps() -> WorkerDeps:
    # Порты файлов, S3 и sniffer тик не трогает: на telegram_send хука нет.
    return WorkerDeps(
        files=None,  # type: ignore[arg-type]
        storage=None,  # type: ignore[arg-type]
        sniffer=None,  # type: ignore[arg-type]
        clock=ManualClock(NOW),
        ugc_bucket="ugc-test",
    )


def _insert_stale_lock(engine: Engine) -> int:
    with engine.begin() as conn:
        job_id = conn.execute(
            text(
                "INSERT INTO jobs (kind, payload, status, attempts, max_attempts, run_after, "
                "locked_by, locked_until, created_at, updated_at) "
                "VALUES ('telegram_send', '{}'::jsonb, 'locked', 1, 6, :ts, 'stale-worker', "
                ":lock, :ts, :ts) RETURNING id"
            ),
            {"ts": NOW - timedelta(hours=2), "lock": NOW - timedelta(hours=1)},
        ).scalar_one()
    return int(job_id)


async def test_scanner_failure_does_not_rollback_unlock_expired(
    uow_factory: Any,
    clean_db: None,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean_booking(sync_engine)
    job_id = _insert_stale_lock(sync_engine)

    async def broken_scanner(repos: Any, now: datetime, limit: int = 100) -> None:
        raise RuntimeError("scanner boom")

    monkeypatch.setattr(scheduler_tick_module, "run_due_scans", broken_scanner)

    # Тик не должен падать: сбой сканера логируется и глотается.
    await scheduler_tick(uow_factory, NOW, _deps())

    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, locked_by FROM jobs WHERE id = :id"), {"id": job_id}
        ).one()
    assert row.status == "queued"
    assert row.locked_by is None
