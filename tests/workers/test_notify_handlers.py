"""Handler'ы notify_reward и notify_ugc_rejected (ADR-31): текст не строится в воркере.

Воркер ставит telegram_send с template + context (ids и причина). Код промокода в payload не
попадает (решение координатора, п.8): проверяем это прямо. Текст собирает /outbox/claim (T12).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.ports.file_sniffer import MimeInfo
from app.workers.context import WorkerDeps
from app.workers.runner import WorkerRunner
from tests.conftest import ManualClock
from tests.factories.ugc import create_admin
from tests.factories.workers import (
    NOW,
    SLUG,
    approve_submission,
    ingest_done,
    jobs_of_kind,
    reject_submission,
    brand_b_submission,
    reward_rows,
)


class _Unused:
    async def fetch(self, tg_file_id: str) -> tuple[bytes, str]:  # pragma: no cover
        raise AssertionError("Telegram не нужен в notify-обработчиках")

    async def put(self, bucket: str, key: str, data: bytes, content_type: str) -> str:
        raise AssertionError("S3 не нужен в notify-обработчиках")

    async def get(self, bucket: str, key: str) -> bytes:  # pragma: no cover
        raise AssertionError("S3 не нужен в notify-обработчиках")

    async def delete(self, bucket: str, key: str) -> None:  # pragma: no cover
        raise AssertionError("S3 не нужен в notify-обработчиках")

    def presigned_get(self, bucket: str, key: str, ttl_seconds: int) -> str:  # pragma: no cover
        raise AssertionError("S3 не нужен в notify-обработчиках")

    def sniff(self, data: bytes) -> MimeInfo:  # pragma: no cover
        raise AssertionError("sniff не нужен в notify-обработчиках")


def _deps() -> WorkerDeps:
    unused = _Unused()
    return WorkerDeps(
        files=unused,
        storage=unused,
        sniffer=unused,
        clock=ManualClock(NOW),
        ugc_bucket="ugc-test",
    )


async def _run(uow_factory: Any, kind: str) -> None:
    runner = WorkerRunner(
        uow_factory, _deps(), kinds=(kind,), worker_id="t-notify", poll_interval_s=0.01
    )
    await runner.run_once()


async def test_notify_reward_enqueues_telegram_send(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T8: notify_reward -> telegram_send(template='reward_notice', reward_ids), без кода."""
    case = await brand_b_submission(uow_factory, sync_engine, 8001)
    await ingest_done(uow_factory, case.submission_id)
    admin_id = create_admin(sync_engine)
    await approve_submission(uow_factory, case.submission_id, admin_id)

    await _run(uow_factory, "notify_reward")

    notify = jobs_of_kind(sync_engine, "notify_reward")
    assert notify[0].status == "done"
    sends = jobs_of_kind(sync_engine, "telegram_send")
    assert len(sends) == 1
    send = sends[0]
    assert send.status == "queued"
    payload = send.payload
    assert payload["chat_id"] == int(case.guest.external_id)
    assert payload["template"] == "reward_notice"
    rewards = reward_rows(sync_engine, case.attempt_id)
    assert sorted(payload["context"]["reward_ids"]) == sorted(r.id for r in rewards)
    serialized = json.dumps(payload, ensure_ascii=False)
    for reward in rewards:
        if reward.code is not None:
            assert reward.code not in serialized


async def test_notify_ugc_rejected_enqueues_reason_context(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T9: notify_ugc_rejected -> telegram_send(template='ugc_rejected', reason, activity_slug)."""
    case = await brand_b_submission(uow_factory, sync_engine, 8002)
    await ingest_done(uow_factory, case.submission_id)
    admin_id = create_admin(sync_engine)
    await reject_submission(uow_factory, case.submission_id, admin_id, "low_quality")

    await _run(uow_factory, "notify_ugc_rejected")

    assert jobs_of_kind(sync_engine, "notify_ugc_rejected")[0].status == "done"
    sends = jobs_of_kind(sync_engine, "telegram_send")
    assert len(sends) == 1
    payload: dict[str, Any] = sends[0].payload
    assert payload["chat_id"] == int(case.guest.external_id)
    assert payload["template"] == "ugc_rejected"
    assert payload["context"] == {
        "reason_slug": "low_quality",
        "attempt_closed": False,
        "activity_slug": SLUG,
    }
    with sync_engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM attempts WHERE id = :id"), {"id": case.attempt_id}
        ).scalar_one()
    assert status == "pending_moderation"
