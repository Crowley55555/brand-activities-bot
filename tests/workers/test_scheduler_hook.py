"""ТОП-2 ревью фазы 3c: dead-строка после истёкшего замка проходит on-dead хук планировщика.

Раньше unlock_expired переводил такую задачу в dead без хука: подача оставалась в receiving и
гость не получал ни файла, ни уведомления. Теперь хук зовётся в той же транзакции тика.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.workers.context import WorkerDeps
from app.workers.scheduler_tick import scheduler_tick
from tests.conftest import ManualClock
from tests.factories.workers import NOW, jobs_of_kind, brand_b_submission, submission_status


def _deps() -> WorkerDeps:
    return WorkerDeps(
        files=None,  # type: ignore[arg-type]
        storage=None,  # type: ignore[arg-type]
        sniffer=None,  # type: ignore[arg-type]
        clock=ManualClock(NOW),
        ugc_bucket="ugc-test",
    )


async def test_unlock_expired_fires_on_dead_hook_for_ingest(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Зависший воркер на последней попытке: тик даёт ingest_failed и notify_ingest_failed."""
    case = await brand_b_submission(uow_factory, sync_engine, 7101)
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET status = 'locked', locked_by = 'w-dead', attempts = 6, "
                "locked_until = now() - interval '1 minute' WHERE kind = 'ingest_ugc_file'"
            )
        )

    released = await scheduler_tick(uow_factory, datetime.now(UTC), _deps())

    assert len(released) == 1
    assert submission_status(sync_engine, case.submission_id) == "ingest_failed"
    notify = jobs_of_kind(sync_engine, "notify_ingest_failed")
    assert len(notify) == 1
    assert notify[0].status == "queued"
    assert notify[0].payload["system_reason_slug"] == "ingest_dead"
    assert notify[0].payload["attempt_id"] == case.attempt_id
    assert jobs_of_kind(sync_engine, "ingest_ugc_file")[0].status == "dead"
