"""Handler polaroid_render (ADR-40, B1, B4): Telegram -> S3 -> рамка -> S3 -> mark_result.

Порядок: согласие первой строкой (правило 1). Статус receiving - файл из Telegram через порт,
сниффинг
MIME и размер по limits, исходник в S3, затем mark_source_stored. Статус queued (повтор после сбоя)
-
исходник берём из S3, Telegram не дёргаем. Рамка - ImageFramePort, результат - в S3, mark_result, и
задача notify_polaroid_ready с chat_id. Терминальные исходы (отзыв согласия, файл не найден или не
то
MIME, битое фото) закрывают подачу и прохождение; временные сбои Telegram и S3 - повтор с backoff.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final, NoReturn

from app.common.errors import ConflictError
from app.core.activities.photo_frame import (
    mark_failed,
    mark_ingest_failed,
    mark_result,
    mark_source_stored,
)
from app.core.activities.photo_frame.types import FrameSubmissionRef
from app.core.attempts import get_attempt
from app.core.catalog import get_activity_by_id
from app.core.consent import require_consent
from app.core.jobs import enqueue
from app.core.jobs.types import JobRecord, RetryableJobError, TerminalBusinessError
from app.core.ports.image_frame import ImageDecodeError, ImageTooLargeError
from app.core.ports.object_storage import ObjectStorageUnavailable
from app.core.ports.telegram_files import (
    TelegramFileNotFound,
    TelegramFileTooLarge,
    TelegramFileTransient,
)
from app.core.types.ids import UserId
from app.db.unit_of_work import UnitOfWork
from app.workers.context import WorkerDeps
from app.workers.handlers.common import telegram_chat_id

RESULT_CONTENT_TYPE: Final[str] = "image/jpeg"
_SOURCE_EXTENSIONS: Final[dict[str, str]] = {"image/jpeg": "jpg", "image/png": "png"}


def _allowed_mimes(limits: Mapping[str, Any]) -> list[str]:
    raw = limits.get("allowed_mimes")
    if not isinstance(raw, list) or not raw:
        raise TerminalBusinessError("activity_limits_invalid")
    return [str(item) for item in raw]


def _max_bytes(limits: Mapping[str, Any]) -> int:
    raw = limits.get("max_file_bytes")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TerminalBusinessError("activity_limits_invalid")
    return raw


async def _ingest_failed(
    uow: UnitOfWork, deps: WorkerDeps, submission_id: int, reason: str, *, notify: bool
) -> NoReturn:
    """Файла нет: ingest_failed (с системным уведомлением или без), затем терминальная задача."""
    repos = uow.repositories
    try:
        await mark_ingest_failed(
            repos.attempts,
            repos.jobs,
            repos.frames,
            submission_id,
            reason,
            deps.clock.now(),
            notify=notify,
        )
    except ConflictError:
        raise TerminalBusinessError("not_receiving") from None
    raise TerminalBusinessError(reason)


async def _render_failed(
    uow: UnitOfWork, deps: WorkerDeps, submission_id: int, reason: str
) -> NoReturn:
    """Сбой рендера (исходник уже в S3): failed, прохождение cancelled, уведомление гостю."""
    repos = uow.repositories
    try:
        await mark_failed(
            repos.attempts, repos.jobs, repos.frames, submission_id, reason, deps.clock.now()
        )
    except ConflictError:
        raise TerminalBusinessError("not_queued") from None
    raise TerminalBusinessError(reason)


async def _fetch_and_store_source(
    uow: UnitOfWork,
    deps: WorkerDeps,
    submission_id: int,
    job: JobRecord,
    limits: Mapping[str, Any],
) -> bytes:
    repos = uow.repositories
    try:
        data, _declared_type = await deps.files.fetch(str(job.payload["tg_file_id"]))
    except TelegramFileNotFound:
        await _ingest_failed(uow, deps, submission_id, "file_not_found", notify=True)
    except TelegramFileTooLarge:
        await _ingest_failed(uow, deps, submission_id, "file_too_large", notify=True)
    except TelegramFileTransient as exc:
        raise RetryableJobError("telegram_unavailable") from exc
    info = deps.sniffer.sniff(data)
    if info.mime not in _allowed_mimes(limits):
        await _ingest_failed(uow, deps, submission_id, "mime_mismatch", notify=True)
    if len(data) > _max_bytes(limits):
        await _ingest_failed(uow, deps, submission_id, "file_too_large", notify=True)
    extension = _SOURCE_EXTENSIONS.get(info.mime, "bin")
    key = f"{submission_id}/source/{uuid.uuid4().hex}.{extension}"
    try:
        await deps.storage.put(deps.ugc_bucket, key, data, info.mime)
    except ObjectStorageUnavailable as exc:
        raise RetryableJobError("storage_unavailable") from exc
    try:
        await mark_source_stored(
            repos.frames, submission_id, deps.ugc_bucket, key, info.mime, len(data)
        )
    except ConflictError:
        raise TerminalBusinessError("not_receiving") from None
    return data


async def _read_stored_source(deps: WorkerDeps, frame: FrameSubmissionRef) -> bytes:
    if frame.source_bucket is None or frame.source_key is None:
        raise TerminalBusinessError("source_missing")
    try:
        return await deps.storage.get(frame.source_bucket, frame.source_key)
    except ObjectStorageUnavailable as exc:
        raise RetryableJobError("storage_unavailable") from exc


async def _telegram_chat_id(uow: UnitOfWork, user_id: UserId) -> int:
    channel = await uow.repositories.channels.find_active_by_user(user_id, "telegram")
    if channel is None:
        raise TerminalBusinessError("no_telegram_channel")
    return telegram_chat_id(channel.external_id)


async def _consent_revoked(uow: UnitOfWork, deps: WorkerDeps, submission_id: int) -> NoReturn:
    """Отзыв согласия (правило 1): гостю не пишем. В queued исходник уже в S3 - это failed (B-4)."""
    repos = uow.repositories
    frame = await repos.frames.get(submission_id)
    if frame is not None and frame.status == "queued":
        try:
            await mark_failed(
                repos.attempts,
                repos.jobs,
                repos.frames,
                submission_id,
                "consent_revoked",
                deps.clock.now(),
                notify=False,
            )
        except ConflictError:
            raise TerminalBusinessError("not_queued") from None
        raise TerminalBusinessError("consent_revoked")
    await _ingest_failed(uow, deps, submission_id, "consent_revoked", notify=False)


async def on_dead(job: JobRecord, uow: UnitOfWork, deps: WorkerDeps) -> None:
    """Исчерпаны попытки (B-4): подача не зависает ни в receiving, ни в queued.

    receiving (файл так и не получен) -> ingest_failed('ingest_dead'); queued (исходник в S3, рендер
    или запись результата падали) -> failed('render_dead'). Оба закрывают прохождение и ставят
    системное уведомление. В финальном статусе хук ничего не делает: состояние уже согласовано.
    """
    repos = uow.repositories
    submission_id = int(job.payload["submission_id"])
    frame = await repos.frames.get(submission_id)
    if frame is None:
        return
    now = deps.clock.now()
    try:
        if frame.status == "receiving":
            await mark_ingest_failed(
                repos.attempts, repos.jobs, repos.frames, submission_id, "ingest_dead", now
            )
        elif frame.status == "queued":
            await mark_failed(
                repos.attempts, repos.jobs, repos.frames, submission_id, "render_dead", now
            )
    except ConflictError:
        return


async def run(job: JobRecord, uow: UnitOfWork, deps: WorkerDeps) -> None:
    repos = uow.repositories
    submission_id = int(job.payload["submission_id"])
    user_id = UserId(int(job.payload["user_id"]))
    try:
        # Правило 1 / F6: согласие - первая проверка обработчика. Без него ни файла, ни записи.
        await require_consent(repos.consents, user_id)
    except ConflictError:
        await _consent_revoked(uow, deps, submission_id)
    frame = await repos.frames.get(submission_id)
    if frame is None:
        raise TerminalBusinessError("frame_not_found")
    if frame.status not in ("receiving", "queued"):
        raise TerminalBusinessError("frame_already_closed")
    attempt = await get_attempt(repos.attempts, frame.attempt_id)
    activity = await get_activity_by_id(repos.catalog, attempt.activity_id)
    limits = activity.limits
    if frame.status == "receiving":
        source_bytes = await _fetch_and_store_source(uow, deps, submission_id, job, limits)
    else:
        source_bytes = await _read_stored_source(deps, frame)
    if deps.image_frame is None:
        raise RuntimeError("image_frame_not_configured")
    try:
        result = deps.image_frame.apply(source_bytes, str(limits.get("frame_slug", "")))
    except (ImageTooLargeError, ImageDecodeError, ValueError):
        await _render_failed(uow, deps, submission_id, "pil_error")
    key = f"{submission_id}/{uuid.uuid4().hex}.jpg"
    try:
        await deps.storage.put(deps.frames_bucket, key, result, RESULT_CONTENT_TYPE)
    except ObjectStorageUnavailable as exc:
        raise RetryableJobError("storage_unavailable") from exc
    try:
        await mark_result(
            repos.attempts,
            repos.catalog,
            repos.rewards,
            repos.frames,
            submission_id,
            deps.frames_bucket,
            key,
            RESULT_CONTENT_TYPE,
            len(result),
            deps.clock.now(),
        )
    except ConflictError:
        raise TerminalBusinessError("not_queued") from None
    chat_id = await _telegram_chat_id(uow, attempt.user_id)
    await enqueue(
        repos.jobs,
        "notify_polaroid_ready",
        {"frame_submission_id": submission_id, "chat_id": chat_id},
        idempotency_key=f"notify_polaroid_ready:{submission_id}",
    )
