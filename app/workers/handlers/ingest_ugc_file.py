"""Handler ingest_ugc_file (ADR-30, канон 04 §4.6, ТОП-1 и ТОП-2 ревью фазы 3c).

Порядок: согласие первой строкой (правило 1, F6 ревью 3a) -> подача в receiving -> файл из
Telegram через порт -> сниффинг MIME и размер по лимитам активности -> S3 put -> pending.
Терминальные исходы: отзыв согласия (без уведомления), 404 Telegram, файл больше лимита Telegram
(400 "file is too big"), MIME или размер не совпали. Кроме отзыва согласия, каждый терминальный
исход - ingest_failed и системное уведомление notify_ingest_failed. Временные: 429/5xx, сеть,
S3 - повтор с backoff; исчерпание попыток - on_ingest_dead (тот же исход, что и системный сбой).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final, NoReturn

from app.common.errors import ConflictError
from app.core.activities.ugc import ingest_file_done, ingest_file_failed
from app.core.attempts import get_attempt
from app.core.catalog import get_activity_by_id
from app.core.consent import require_consent
from app.core.jobs.types import JobRecord, RetryableJobError, TerminalBusinessError
from app.core.ports.object_storage import ObjectStorageUnavailable
from app.core.ports.telegram_files import (
    TelegramFileNotFound,
    TelegramFileTooLarge,
    TelegramFileTransient,
)
from app.core.types.ids import UgcSubmissionId, UserId
from app.db.unit_of_work import UnitOfWork
from app.workers.context import WorkerDeps

_EXTENSIONS: Final[Mapping[str, str]] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def _allowed_mimes(limits: Mapping[str, Any]) -> frozenset[str]:
    raw = limits.get("allowed_mimes")
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(item) for item in raw)


def _max_bytes(limits: Mapping[str, Any]) -> int:
    value = limits.get("max_file_bytes")
    return int(value) if isinstance(value, int) else 0


async def _fail(
    uow: UnitOfWork,
    deps: WorkerDeps,
    submission_id: UgcSubmissionId,
    reason: str,
    *,
    notify_system: bool,
) -> NoReturn:
    """Подача -> ingest_failed (с системным уведомлением или без), затем терминальная задача."""
    repos = uow.repositories
    try:
        await ingest_file_failed(
            repos.ugc,
            repos.attempts,
            repos.catalog,
            repos.jobs,
            submission_id,
            reason,
            deps.clock.now(),
            notify_system=notify_system,
        )
    except ConflictError:
        raise TerminalBusinessError("not_receiving") from None
    raise TerminalBusinessError(reason)


async def run(job: JobRecord, uow: UnitOfWork, deps: WorkerDeps) -> None:
    repos = uow.repositories
    try:
        # Правило 1 / F6: первая проверка обработчика - согласие. Без него ни файла, ни записи.
        await require_consent(repos.consents, UserId(int(job.payload["user_id"])))
    except ConflictError:
        await _fail(
            uow,
            deps,
            UgcSubmissionId(int(job.payload["submission_id"])),
            "consent_revoked",
            notify_system=False,
        )

    submission_id = UgcSubmissionId(int(job.payload["submission_id"]))
    submission = await repos.ugc.get(submission_id)
    if submission is None or submission.status != "receiving":
        raise TerminalBusinessError("not_receiving")
    attempt = await get_attempt(repos.attempts, submission.attempt_id)
    activity = await get_activity_by_id(repos.catalog, attempt.activity_id)
    limits = activity.limits

    try:
        data, _declared_type = await deps.files.fetch(str(job.payload["tg_file_id"]))
    except TelegramFileNotFound:
        await _fail(uow, deps, submission_id, "file_not_found", notify_system=True)
    except TelegramFileTooLarge:
        await _fail(uow, deps, submission_id, "file_too_large", notify_system=True)
    except TelegramFileTransient as exc:
        raise RetryableJobError("telegram_unavailable") from exc

    info = deps.sniffer.sniff(data)
    if info.mime not in _allowed_mimes(limits):
        await _fail(uow, deps, submission_id, "mime_mismatch", notify_system=True)
    if len(data) > _max_bytes(limits):
        await _fail(uow, deps, submission_id, "file_too_large", notify_system=True)

    extension = _EXTENSIONS.get(info.mime, "bin")
    key = f"{int(submission_id)}/{uuid.uuid4().hex}.{extension}"
    try:
        await deps.storage.put(deps.ugc_bucket, key, data, info.mime)
    except ObjectStorageUnavailable as exc:
        raise RetryableJobError("storage_unavailable") from exc
    try:
        await ingest_file_done(repos.ugc, submission_id, deps.ugc_bucket, key, info.mime, len(data))
    except ConflictError:
        raise TerminalBusinessError("not_receiving") from None


async def on_ingest_dead(job: JobRecord, uow: UnitOfWork, deps: WorkerDeps) -> None:
    """Исчерпаны попытки (или истёкший замок на последней попытке, ТОП-2): ingest_failed.

    Системное уведомление ставится в той же транзакции (канон 04 §4.6). Если подача уже не
    receiving (ingest прошёл раньше, чем задача умерла), состояние согласовано: хук ничего
    не делает.
    """
    repos = uow.repositories
    try:
        await ingest_file_failed(
            repos.ugc,
            repos.attempts,
            repos.catalog,
            repos.jobs,
            UgcSubmissionId(int(job.payload["submission_id"])),
            "ingest_dead",
            deps.clock.now(),
            notify_system=True,
        )
    except ConflictError:
        return
