"""Polaroid photo frame (ADR-40, фаза 5): T5-T7 через core-дверь, handler и настоящий Postgres.

Telegram, хранилище и рамка - стабы портов (правило 4); PIL здесь не нужен: рамку проверяет
tests/adapters/test_pillow_image_frame.py. Задача polaroid_render идёт через WorkerRunner.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Engine

from app.core.activities.photo_frame import FrameAcceptResult, accept_source
from app.core.ports.file_sniffer import MimeInfo
from app.core.ports.image_frame import ImageDecodeError
from app.core.types.ids import ChannelAccountId, UserId
from app.workers.context import WorkerDeps
from app.workers.runner import WorkerRunner
from tests.conftest import ManualClock
from tests.factories import create_guest
from tests.factories.brand_d import (
    PHOTO_LIMITS,
    BrandDActivity,
    attempt_status,
    frame_rows,
    make_activity,
)
from tests.factories.ugc import job_rows, reward_rows

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
TG_FILE_ID = "AgACAgIAAxkBAAPOLAROID"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
RESULT = b"\xff\xd8\xff\xe0polaroid-result\xff\xd9"
SOURCE_BUCKET = "ugc-test"
FRAMES_BUCKET = "frames-test"


class StubFiles:
    """TelegramFilesPort: отдаёт фото гостя без сети."""

    async def fetch(self, tg_file_id: str) -> tuple[bytes, str]:
        return JPEG, "image/jpeg"


class MemStorage:
    """ObjectStoragePort в памяти: put/get по (bucket, key)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}

    async def put(self, bucket: str, key: str, data: bytes, content_type: str) -> str:
        self.objects[(bucket, key)] = (data, content_type)
        return f"mem://{bucket}/{key}"

    async def get(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)][0]

    async def delete(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)

    def presigned_get(self, bucket: str, key: str, ttl_seconds: int) -> str:
        return f"mem://{bucket}/{key}?ttl={ttl_seconds}"


class StubSniffer:
    def sniff(self, data: bytes) -> MimeInfo:
        return MimeInfo(mime="image/jpeg", is_image=True)


class StubFrame:
    """ImageFramePort-стаб: возвращает результат или бросает заданную ошибку."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc
        self.calls: list[str] = []

    def apply(self, src_bytes: bytes, frame_slug: str) -> bytes:
        self.calls.append(frame_slug)
        if self._exc is not None:
            raise self._exc
        return RESULT


async def accept_as(uow_factory: Any, guest: Any, activity: BrandDActivity) -> FrameAcceptResult:
    async with uow_factory() as uow:
        repos = uow.repositories
        return await accept_source(
            repos.consents,
            repos.catalog,
            repos.attempts,
            repos.frames,
            repos.jobs,
            UserId(guest.user_id),
            activity.slug,
            ChannelAccountId(guest.channel_account_id),
            TG_FILE_ID,
            "image/jpeg",
            len(JPEG),
            NOW,
        )


async def run_polaroid(uow_factory: Any, storage: MemStorage, frame: StubFrame) -> int:
    deps = WorkerDeps(
        files=StubFiles(),
        storage=storage,
        sniffer=StubSniffer(),
        clock=ManualClock(NOW),
        ugc_bucket=SOURCE_BUCKET,
        frames_bucket=FRAMES_BUCKET,
        image_frame=frame,
    )
    runner = WorkerRunner(
        uow_factory,
        deps,
        kinds=("polaroid_render",),
        worker_id="t-polaroid",
        poll_interval_s=0.01,
    )
    return await runner.run_once()


async def test_accept_source_creates_receiving_and_enqueues_render(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T5: accept_source - строка receiving без файла, прохождение на модерации, job
    polaroid_render."""
    guest = create_guest(sync_engine, external_id="3101", now=NOW)
    activity = make_activity(sync_engine, "photo_frame", PHOTO_LIMITS)
    accepted = await accept_as(uow_factory, guest, activity)

    assert accepted.status == "receiving"
    rows = frame_rows(sync_engine, accepted.attempt_id)
    assert len(rows) == 1
    assert rows[0].status == "receiving"
    assert rows[0].source_bucket is None
    assert rows[0].source_size_bytes is None
    assert attempt_status(sync_engine, accepted.attempt_id) == "pending_moderation"
    jobs = job_rows(sync_engine, "polaroid_render")
    assert [int(job.payload["submission_id"]) for job in jobs] == [accepted.submission_id]


async def test_render_happy_path_marks_result_and_grants_physical_gift(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T6: mock рамки -> done + result; reward physical_gift; notify_polaroid_ready поставлен."""
    guest = create_guest(sync_engine, external_id="3102", now=NOW)
    activity = make_activity(sync_engine, "photo_frame", PHOTO_LIMITS)
    accepted = await accept_as(uow_factory, guest, activity)
    storage = MemStorage()
    frame = StubFrame()

    assert await run_polaroid(uow_factory, storage, frame) == 1

    row = frame_rows(sync_engine, accepted.attempt_id)[0]
    assert row.status == "done"
    assert row.result_key is not None
    assert row.source_bucket == SOURCE_BUCKET
    assert frame.calls == ["brand_c_polaroid"]
    assert storage.objects[(FRAMES_BUCKET, row.result_key)] == (RESULT, "image/jpeg")
    rewards = reward_rows(sync_engine, accepted.attempt_id)
    assert [item.reward_type for item in rewards] == ["physical_gift"]
    assert attempt_status(sync_engine, accepted.attempt_id) == "completed"
    notify = job_rows(sync_engine, "notify_polaroid_ready")
    assert len(notify) == 1
    assert int(notify[0].payload["frame_submission_id"]) == accepted.submission_id


async def test_render_pil_error_marks_failed_and_cancels_attempt(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T7: ошибка Pillow -> failed (pil_error), прохождение cancelled, системное уведомление."""
    guest = create_guest(sync_engine, external_id="3103", now=NOW)
    activity = make_activity(sync_engine, "photo_frame", PHOTO_LIMITS)
    accepted = await accept_as(uow_factory, guest, activity)

    assert await run_polaroid(uow_factory, MemStorage(), StubFrame(ImageDecodeError("broken"))) == 1

    row = frame_rows(sync_engine, accepted.attempt_id)[0]
    assert row.status == "failed"
    assert row.failure_reason == "pil_error"
    assert row.result_key is None
    assert attempt_status(sync_engine, accepted.attempt_id) == "cancelled"
    assert reward_rows(sync_engine, accepted.attempt_id) == []
    notify = job_rows(sync_engine, "notify_ingest_failed")
    assert len(notify) == 1
    assert notify[0].payload["system_reason_slug"] == "polaroid_render_failed"
    assert notify[0].payload["attempt_closed"] is True
