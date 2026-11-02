"""Polaroid photo frame (ADR-40, фаза 5): приёмка в два шага через задачу polaroid_render.

Правило 1: согласие - первой строкой accept_source. accept_source создаёт строку receiving без
исходника и ставит задачу polaroid_render (отдельный kind: у Polaroid нет модерации). Задача сама
скачивает файл из Telegram, кладёт исходник в S3 (mark_source_stored), рисует рамку и пишет
результат
(mark_result). Сбой после исходника - mark_failed, до исходника - mark_ingest_failed; оба закрывают
прохождение (cancelled) и при notify ставят системное уведомление.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from app.common.errors import ConflictError, ValidationError
from app.core.activities.photo_frame.repository import FrameRepo
from app.core.activities.photo_frame.types import FrameAcceptResult, FrameSubmissionRef
from app.core.attempts import (
    AttemptRepo,
    cancel_from_moderation,
    close_from_moderation,
    get_attempt,
    mark_pending_moderation,
    start_attempt,
)
from app.core.catalog import CatalogRepo, get_activity_by_id, get_activity_by_slug
from app.core.consent import ConsentRepo, require_consent
from app.core.jobs import JobRepo, enqueue
from app.core.rewards import RewardRepo, grant_on_completion
from app.core.types.dto import GrantSpec
from app.core.types.ids import ChannelAccountId, UserId

ACTIVITY_KIND: Final = "photo_frame"
# Ключ activities.limits.system_reject_reasons для сбоя рендера (U4, миграция 0014).
RENDER_FAILED_SLUG: Final = "polaroid_render_failed"
CANCEL_REASON: Final = "polaroid_failed"
PHYSICAL_GIFT: Final = "physical_gift"


def _limit_list(limits: Mapping[str, Any], key: str) -> list[str]:
    value = limits.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise ValidationError("activity_limits_invalid")
    return [str(v) for v in value]


def _limit_int(limits: Mapping[str, Any], key: str) -> int:
    value = limits.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("activity_limits_invalid")
    return value


def check_declared_file(limits: Mapping[str, Any], mime_type: str, file_size_bytes: int) -> None:
    """Декларация из Telegram против limits (allowed_mimes, max_file_bytes): до любой записи."""
    if file_size_bytes <= 0:
        raise ValidationError("file_size_invalid")
    if mime_type not in _limit_list(limits, "allowed_mimes"):
        raise ValidationError("mime_not_allowed")
    if file_size_bytes > _limit_int(limits, "max_file_bytes"):
        raise ValidationError("file_too_large")


async def accept_source(
    consents: ConsentRepo,
    catalog: CatalogRepo,
    attempts: AttemptRepo,
    frames: FrameRepo,
    jobs: JobRepo,
    user_id: UserId,
    activity_slug: str,
    channel_account_id: ChannelAccountId,
    tg_file_id: str,
    mime_type: str,
    file_size_bytes: int,
    now: datetime,
) -> FrameAcceptResult:
    """Приём фото: строка receiving и задача polaroid_render; прохождение на модерации (U1)."""
    await require_consent(consents, user_id)
    if not tg_file_id.strip():
        raise ValidationError("tg_file_id_required")
    activity = await get_activity_by_slug(catalog, activity_slug)
    if activity.kind != ACTIVITY_KIND:
        raise ValidationError("activity_kind_mismatch")
    check_declared_file(activity.limits, mime_type, file_size_bytes)

    started = await start_attempt(
        consents,
        catalog,
        attempts,
        user_id,
        activity_slug,
        channel_account_id,
        now,
        expected_kind=ACTIVITY_KIND,
    )
    await mark_pending_moderation(attempts, started.attempt_id)
    frame = await frames.insert_receiving(started.attempt_id, now)
    await enqueue(
        jobs,
        "polaroid_render",
        {"submission_id": int(frame.id), "user_id": int(user_id), "tg_file_id": tg_file_id},
        idempotency_key=f"polaroid_render:{int(frame.id)}",
    )
    return FrameAcceptResult(
        submission_id=int(frame.id), attempt_id=started.attempt_id, status=frame.status
    )


async def mark_source_stored(
    frames: FrameRepo,
    submission_id: int,
    bucket: str,
    key: str,
    content_type: str,
    size_bytes: int,
) -> FrameSubmissionRef:
    stored = await frames.set_source_stored(submission_id, bucket, key, content_type, size_bytes)
    if stored is None:
        raise ConflictError("not_receiving")
    return stored


async def mark_result(
    attempts: AttemptRepo,
    catalog: CatalogRepo,
    rewards: RewardRepo,
    frames: FrameRepo,
    submission_id: int,
    bucket: str,
    key: str,
    content_type: str,
    size_bytes: int,
    now: datetime,
) -> FrameSubmissionRef:
    """Результат готов: done, прохождение completed, физический подарок (ADR-40)."""
    done = await frames.set_result(submission_id, bucket, key, content_type, size_bytes, now)
    if done is None:
        raise ConflictError("not_queued")
    attempt = await get_attempt(attempts, done.attempt_id)
    activity = await get_activity_by_id(catalog, attempt.activity_id)
    await close_from_moderation(attempts, done.attempt_id, "completed", None, now)
    await grant_on_completion(
        rewards,
        done.attempt_id,
        [GrantSpec(reward_type="physical_gift", title=activity.display_name)],
        now,
    )
    return done


async def _close_and_notify(
    attempts: AttemptRepo,
    jobs: JobRepo,
    frame: FrameSubmissionRef,
    now: datetime,
    *,
    notify: bool,
) -> None:
    await cancel_from_moderation(attempts, frame.attempt_id, CANCEL_REASON, now)
    if not notify:
        return
    attempt = await get_attempt(attempts, frame.attempt_id)
    await enqueue(
        jobs,
        "notify_ingest_failed",
        {
            "user_id": int(attempt.user_id),
            "attempt_id": int(frame.attempt_id),
            "system_reason_slug": RENDER_FAILED_SLUG,
            "attempt_closed": True,
        },
        run_after=now,
        idempotency_key=f"ingest_failed:frame:{frame.id}",
    )


async def mark_failed(
    attempts: AttemptRepo,
    jobs: JobRepo,
    frames: FrameRepo,
    submission_id: int,
    reason: str,
    now: datetime,
    *,
    notify: bool = True,
) -> FrameSubmissionRef:
    """Сбой после исходника: failed, прохождение cancelled; notify=False после отзыва согласия."""
    failed = await frames.set_failed(submission_id, reason, now)
    if failed is None:
        raise ConflictError("not_queued")
    await _close_and_notify(attempts, jobs, failed, now, notify=notify)
    return failed


async def mark_ingest_failed(
    attempts: AttemptRepo,
    jobs: JobRepo,
    frames: FrameRepo,
    submission_id: int,
    reason: str,
    now: datetime,
    *,
    notify: bool = True,
) -> FrameSubmissionRef:
    """Сбой получения файла (исходника нет): ingest_failed; notify=False при отзыве согласия."""
    failed = await frames.set_ingest_failed(submission_id, reason, now)
    if failed is None:
        raise ConflictError("not_receiving")
    await _close_and_notify(attempts, jobs, failed, now, notify=notify)
    return failed
