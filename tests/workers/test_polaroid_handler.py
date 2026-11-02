"""Обработчик polaroid_render на сбоях (B-4, фаза 5): on-dead из queued, отзыв согласия в queued.

Идём через настоящий Postgres и стабы портов из tests/activities/test_photo_frame.py. Гостю при
отзыве согласия не пишем (решение 5 ревью UGC); при on-dead - системное уведомление.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Engine

from app.core.activities.photo_frame import mark_source_stored
from app.core.jobs.types import JobRecord
from app.core.types.ids import JobId
from app.workers.context import WorkerDeps
from app.workers.handlers import polaroid_render
from app.workers.runner import WorkerRunner
from tests.activities.test_photo_frame import (
    FRAMES_BUCKET,
    JPEG,
    NOW,
    SOURCE_BUCKET,
    MemStorage,
    StubFiles,
    StubFrame,
    StubSniffer,
    accept_as,
)
from tests.conftest import ManualClock
from tests.factories import create_guest, revoke_consent
from tests.factories.brand_d import (
    PHOTO_LIMITS,
    attempt_status,
    frame_rows,
    make_activity,
)
from tests.factories.ugc import job_rows


def _deps(now: datetime = NOW) -> WorkerDeps:
    return WorkerDeps(
        files=StubFiles(),
        storage=MemStorage(),
        sniffer=StubSniffer(),
        clock=ManualClock(now),
        ugc_bucket=SOURCE_BUCKET,
        frames_bucket=FRAMES_BUCKET,
        image_frame=StubFrame(),
    )


async def _accepted_and_stored(uow_factory: Any, guest: Any, activity: Any) -> Any:
    """Приём и исходник в S3: подача в статусе queued (рендер ещё не делали)."""
    accepted = await accept_as(uow_factory, guest, activity)
    async with uow_factory() as uow:
        await mark_source_stored(
            uow.repositories.frames,
            accepted.submission_id,
            SOURCE_BUCKET,
            "src/1.jpg",
            "image/jpeg",
            len(JPEG),
        )
    return accepted


def _dead_job(engine: Engine) -> JobRecord:
    row = job_rows(engine, "polaroid_render")[0]
    return JobRecord(
        id=JobId(int(row.id)),
        kind=str(row.kind),
        payload=row.payload,
        status="dead",
        attempts=6,
        max_attempts=6,
        idempotency_key=row.idempotency_key,
        last_error=None,
    )


async def test_dead_hook_marks_failed_from_queued(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """on-dead из queued: failed('render_dead'), прохождение cancelled, гость получает сообщение."""
    guest = create_guest(sync_engine, external_id="3601", now=NOW)
    activity = make_activity(sync_engine, "photo_frame", PHOTO_LIMITS)
    accepted = await _accepted_and_stored(uow_factory, guest, activity)

    # Уведомление ставится с run_after = clock.now(): берём реальное время, иначе claim его не
    # увидит.
    real_now = datetime.now(UTC)
    async with uow_factory() as uow:
        await polaroid_render.on_dead(_dead_job(sync_engine), uow, _deps(real_now))

    row = frame_rows(sync_engine, accepted.attempt_id)[0]
    assert row.status == "failed"
    assert row.failure_reason == "render_dead"
    assert attempt_status(sync_engine, accepted.attempt_id) == "cancelled"

    runner = WorkerRunner(
        uow_factory,
        _deps(real_now),
        kinds=("notify_ingest_failed",),
        worker_id="t-dead",
        poll_interval_s=0.01,
    )
    assert await runner.run_once() == 1
    sends = job_rows(sync_engine, "telegram_send")
    assert len(sends) == 1
    assert sends[0].payload["template"] == "ingest_failed"
    assert sends[0].payload["context"]["reason_slug"] == "polaroid_render_failed"


async def test_consent_revoked_in_queued_marks_failed(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Отзыв согласия при queued: failed('consent_revoked') без ConflictError и без уведомления."""
    guest = create_guest(sync_engine, external_id="3602", now=NOW)
    activity = make_activity(sync_engine, "photo_frame", PHOTO_LIMITS)
    accepted = await _accepted_and_stored(uow_factory, guest, activity)
    revoke_consent(sync_engine, guest, NOW)

    runner = WorkerRunner(
        uow_factory,
        _deps(),
        kinds=("polaroid_render",),
        worker_id="t-revoked",
        poll_interval_s=0.01,
    )
    assert await runner.run_once() == 1

    row = frame_rows(sync_engine, accepted.attempt_id)[0]
    assert row.status == "failed"
    assert row.failure_reason == "consent_revoked"
    assert attempt_status(sync_engine, accepted.attempt_id) == "cancelled"
    assert job_rows(sync_engine, "notify_ingest_failed") == []
    job = job_rows(sync_engine, "polaroid_render")[0]
    assert job.status == "done"
    assert "terminal_business_error:consent_revoked" in (job.last_error or "")
