"""Очередь jobs фазы 3c: SKIP LOCKED, ack с fencing, unlock и scheduler_tick (ADR-26, ADR-28).

T1-T4 - приёмочные из промта (T3/T4 обновлены под dead-only unlock_expired, ТОП-2).
Остальные - контракт ack_* с fencing (ТОП-3) и backoff (канон 04 §3.3).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.common.errors import ConflictError
from app.core.jobs import (
    ack_dead,
    ack_retryable,
    ack_success,
    ack_terminal_business,
    claim_batch,
    enqueue,
    unlock_expired,
)
from app.core.jobs.backoff import backoff_seconds
from app.core.jobs.types import JobRecord
from app.workers.context import WorkerDeps
from app.workers.scheduler_tick import scheduler_tick
from tests.conftest import ManualClock
from tests.factories.workers import NOW

KIND = "notify_reward"
WORKER = "w-test"


def _deps() -> WorkerDeps:
    """Для тестов очереди порты не нужны: хуков на dead-строках здесь нет."""
    return WorkerDeps(
        files=None,  # type: ignore[arg-type]
        storage=None,  # type: ignore[arg-type]
        sniffer=None,  # type: ignore[arg-type]
        clock=ManualClock(NOW),
        ugc_bucket="ugc",
    )


async def _seed(uow_factory: Any, count: int, prefix: str) -> None:
    async with uow_factory() as uow:
        for i in range(count):
            await enqueue(
                uow.repositories.jobs,
                KIND,
                {"reward_id": i + 1},
                idempotency_key=f"{prefix}:{i}",
            )


async def _claim_one(uow_factory: Any, worker: str = WORKER) -> JobRecord:
    async with uow_factory() as uow:
        records = await claim_batch(uow.repositories.jobs, worker, [KIND], 1, 90)
    assert len(records) == 1
    return records[0]


def _set(engine: Engine, job_id: int, **columns: Any) -> None:
    assets = ", ".join(f"{name} = :{name}" for name in columns)
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE jobs SET {assets} WHERE id = :id"), {"id": job_id, **columns})


def _state(engine: Engine, job_id: int) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT status, attempts, last_error, locked_by, "
                "EXTRACT(EPOCH FROM (run_after - now())) AS delay_s FROM jobs WHERE id = :id"
            ),
            {"id": job_id},
        ).one()


async def test_claim_batch_skip_locked_concurrent(uow_factory: Any, clean_db: None) -> None:
    """T1: три параллельных claim_batch берут непересекающиеся наборы, вместе - все 10."""
    await _seed(uow_factory, 10, "t1")

    async def claim(worker: str) -> list[int]:
        async with uow_factory() as uow:
            records = await claim_batch(uow.repositories.jobs, worker, [KIND], 4, 90)
            return [int(record.id) for record in records]

    first, second, third = await asyncio.gather(claim("w1"), claim("w2"), claim("w3"))
    assert not set(first) & set(second)
    assert not set(first) & set(third)
    assert not set(second) & set(third)
    assert len(first) + len(second) + len(third) == 10


async def _claim_kind(uow_factory: Any, kind: str, worker: str) -> JobRecord:
    async with uow_factory() as uow:
        records = await claim_batch(uow.repositories.jobs, worker, [kind], 1, 90)
    assert len(records) == 1
    return records[0]


def _set_kind(engine: Engine, kind: str, **columns: Any) -> None:
    assets = ", ".join(f"{name} = :{name}" for name in columns)
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE jobs SET {assets} WHERE kind = :kind"), {"kind": kind, **columns}
        )


async def test_ack_retryable_schedules_backoff(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T2: attempts=1 -> ~60 с; attempts=5 -> ~16 мин; attempts=6 -> dead (канон 04 §3.3).

    Каждый случай - отдельный вид задачи, чтобы claim брал ровно ту строку, что нужна.
    """
    async with uow_factory() as uow:
        for kind in ("notify_reward", "notify_ugc_rejected", "s3_delete"):
            await enqueue(uow.repositories.jobs, kind, {"case": kind}, idempotency_key=f"t2:{kind}")

    first = await _claim_kind(uow_factory, "notify_reward", "w-t2")
    assert first.attempts == 1
    async with uow_factory() as uow:
        outcome = await ack_retryable(uow.repositories.jobs, first.id, "w-t2", "boom")
    assert outcome == "queued"
    delay = float(_state(sync_engine, int(first.id)).delay_s)
    assert 60 * 0.8 - 1 <= delay <= 60 * 1.2 + 1

    _set_kind(sync_engine, "notify_ugc_rejected", attempts=4)
    fifth = await _claim_kind(uow_factory, "notify_ugc_rejected", "w-t2")
    assert fifth.attempts == 5
    async with uow_factory() as uow:
        await ack_retryable(uow.repositories.jobs, fifth.id, "w-t2", "boom")
    delay = float(_state(sync_engine, int(fifth.id)).delay_s)
    assert 960 * 0.8 - 1 <= delay <= 960 * 1.2 + 1

    _set_kind(sync_engine, "s3_delete", attempts=5)
    sixth = await _claim_kind(uow_factory, "s3_delete", "w-t2")
    assert sixth.attempts == 6
    async with uow_factory() as uow:
        outcome = await ack_retryable(uow.repositories.jobs, sixth.id, "w-t2", "boom")
    assert outcome == "dead"
    assert _state(sync_engine, int(sixth.id)).status == "dead"


async def test_unlock_expired_releases_stuck_jobs(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T3: locked с истёкшим locked_until возвращается в queued; dead-список пуст (ТОП-2)."""
    await _seed(uow_factory, 1, "t3")
    job = await _claim_one(uow_factory)
    _set(sync_engine, int(job.id), locked_until=datetime(2020, 1, 1, tzinfo=UTC))

    async with uow_factory() as uow:
        dead = await unlock_expired(uow.repositories.jobs, datetime.now(UTC))
    assert dead == []
    assert _state(sync_engine, int(job.id)).status == "queued"


async def test_scheduler_tick_runs_unlock_expired(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T4: scheduler_tick снимает истёкшую блокировку (статус queued, без dead-хука)."""
    await _seed(uow_factory, 1, "t4")
    job = await _claim_one(uow_factory)
    _set(sync_engine, int(job.id), locked_until=datetime(2020, 1, 1, tzinfo=UTC))

    dead = await scheduler_tick(uow_factory, datetime.now(UTC), _deps())
    assert dead == []
    assert _state(sync_engine, int(job.id)).status == "queued"


async def test_ack_success_on_unlocked_job_conflicts(uow_factory: Any, clean_db: None) -> None:
    """Задача не locked: ack с любым worker_id - fencing_failed (ТОП-3)."""
    await _seed(uow_factory, 1, "ack-ok")
    async with uow_factory() as uow:
        queued = await uow.repositories.jobs.id_by_idempotency_key("ack-ok:0")
    assert queued is not None
    with pytest.raises(ConflictError) as exc:
        async with uow_factory() as uow:
            await ack_success(uow.repositories.jobs, queued, WORKER)
    assert exc.value.code == "fencing_failed"


async def test_ack_from_wrong_worker_fails_fencing(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """ТОП-3: job locked_by=w-test; ack от w-other -> fencing_failed, статус не меняется."""
    await _seed(uow_factory, 1, "fence")
    job = await _claim_one(uow_factory, worker=WORKER)

    with pytest.raises(ConflictError) as exc:
        async with uow_factory() as uow:
            await ack_success(uow.repositories.jobs, job.id, "w-other")
    assert exc.value.code == "fencing_failed"
    state = _state(sync_engine, int(job.id))
    assert state.status == "locked"
    assert state.locked_by == WORKER

    with pytest.raises(ConflictError):
        async with uow_factory() as uow:
            await ack_retryable(uow.repositories.jobs, job.id, "w-other", "boom")
    assert _state(sync_engine, int(job.id)).status == "locked"


async def test_ack_terminal_business_writes_marker(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    await _seed(uow_factory, 1, "tb")
    job = await _claim_one(uow_factory)
    async with uow_factory() as uow:
        await ack_terminal_business(uow.repositories.jobs, job.id, WORKER, "user_blocked")
    state = _state(sync_engine, int(job.id))
    assert state.status == "done"
    assert state.last_error == "terminal_business_error:user_blocked"


async def test_ack_retryable_honours_retry_after(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Retry-After Telegram: run_after = now + retry_after_s точно (канон 04 §3.3)."""
    await _seed(uow_factory, 1, "ra")
    job = await _claim_one(uow_factory)
    async with uow_factory() as uow:
        await ack_retryable(uow.repositories.jobs, job.id, WORKER, "flood", retry_after_s=7)
    delay = float(_state(sync_engine, int(job.id)).delay_s)
    assert 6 <= delay <= 7.5


async def test_ack_retryable_accepts_zero_retry_after(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Ревью F: retry_after_s=0 - мгновенный повтор, а не ошибка."""
    await _seed(uow_factory, 1, "ra0")
    job = await _claim_one(uow_factory)
    async with uow_factory() as uow:
        outcome = await ack_retryable(
            uow.repositories.jobs, job.id, WORKER, "flood", retry_after_s=0
        )
    assert outcome == "queued"
    assert float(_state(sync_engine, int(job.id)).delay_s) <= 0.5


async def test_unlock_expired_kills_exhausted_job(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Исчерпавшая max_attempts задача при истечении блокировки уходит в dead и возвращается."""
    await _seed(uow_factory, 1, "exh")
    job = await _claim_one(uow_factory)
    _set(
        sync_engine,
        int(job.id),
        attempts=6,
        locked_until=datetime(2020, 1, 1, tzinfo=UTC),
    )
    async with uow_factory() as uow:
        dead = await unlock_expired(uow.repositories.jobs, datetime.now(UTC))
    assert [record.id for record in dead] == [job.id]
    assert dead[0].kind == KIND
    assert _state(sync_engine, int(job.id)).status == "dead"


async def test_ack_dead_sets_dead_and_error(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    await _seed(uow_factory, 1, "dead")
    job = await _claim_one(uow_factory)
    async with uow_factory() as uow:
        await ack_dead(uow.repositories.jobs, job.id, WORKER, "poison")
    state = _state(sync_engine, int(job.id))
    assert state.status == "dead"
    assert state.last_error == "poison"


def test_backoff_formula_matches_canon() -> None:
    """min(60·2^(attempts-1), 2h) с джиттером: без джиттера - точные значения канона."""
    assert backoff_seconds(1, jitter=1.0) == 60
    assert backoff_seconds(2, jitter=1.0) == 120
    assert backoff_seconds(5, jitter=1.0) == 960
    assert backoff_seconds(12, jitter=1.0) == 7200
