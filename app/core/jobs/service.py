"""Use-case'ы очереди jobs: постановка, claim, ack с fencing, unlock (ADR-26, канон 04 §3).

Fencing (ТОП-3 ревью фазы 3c): любой ack проходит только если задача locked именно этим воркером.
Иначе - ConflictError("fencing_failed"): старый воркер после истечения замка не перезаписывает
исход задачи, которую уже взял другой воркер.
"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Literal

from app.common.errors import ConflictError, ValidationError
from app.core.jobs.backoff import backoff_seconds
from app.core.jobs.repository import JobRepo
from app.core.jobs.types import JOB_KINDS, TERMINAL_MARKER_PREFIX, JobKind, JobRecord
from app.core.types.ids import JobId

_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9_]{1,64}")
_ERROR_MAX_LENGTH: Final[int] = 500
FENCING_FAILED: Final[str] = "fencing_failed"


async def enqueue(
    repo: JobRepo,
    kind: JobKind,
    payload: Mapping[str, Any],
    run_after: datetime | None = None,
    idempotency_key: str | None = None,
) -> JobId:
    """Поставить задачу в очередь (ADR-18).

    Повторная постановка с тем же `idempotency_key` не создаёт вторую задачу: возвращается id
    уже стоящей (04 §4.2). Без ключа повтор - это новая задача, как и задумано.
    """
    if kind not in JOB_KINDS:
        raise ValidationError("job_kind_unknown")
    job_id = await repo.insert(kind, payload, run_after, idempotency_key)
    if job_id is not None:
        return job_id
    if idempotency_key is None:
        raise ConflictError("job_insert_conflict")
    existing = await repo.id_by_idempotency_key(idempotency_key)
    if existing is None:
        raise ConflictError("job_insert_conflict")
    return existing


async def claim_batch(
    repo: JobRepo,
    worker_id: str,
    kinds: Sequence[str],
    limit: int,
    visibility_timeout_s: int,
) -> list[JobRecord]:
    """Взять до `limit` готовых задач заданных видов; блокировка на visibility_timeout_s секунд."""
    if not kinds or not set(kinds) <= JOB_KINDS:
        raise ValidationError("job_kind_unknown")
    if limit < 1 or visibility_timeout_s < 1:
        raise ValidationError("claim_params_invalid")
    return await repo.claim(worker_id, tuple(kinds), limit, visibility_timeout_s)


async def ack_success(repo: JobRepo, job_id: JobId, worker_id: str) -> None:
    if not await repo.mark_done(job_id, worker_id, None):
        raise ConflictError(FENCING_FAILED)


async def ack_terminal_business(repo: JobRepo, job_id: JobId, worker_id: str, slug: str) -> None:
    """Бизнес-отказ: done с маркером terminal_business_error:<slug> (канон 04 §3.2)."""
    if _SLUG_RE.fullmatch(slug) is None:
        raise ValidationError("terminal_slug_invalid")
    if not await repo.mark_done(job_id, worker_id, f"{TERMINAL_MARKER_PREFIX}{slug}"):
        raise ConflictError(FENCING_FAILED)


async def ack_dead(repo: JobRepo, job_id: JobId, worker_id: str, error: str) -> None:
    if not await repo.mark_dead(job_id, worker_id, error[:_ERROR_MAX_LENGTH]):
        raise ConflictError(FENCING_FAILED)


async def ack_retryable(
    repo: JobRepo,
    job_id: JobId,
    worker_id: str,
    error: str,
    retry_after_s: int | None = None,
    jitter: float | None = None,
) -> Literal["queued", "dead"]:
    """Временный сбой. attempts >= max_attempts - dead; иначе queued с backoff или Retry-After.

    retry_after_s (Retry-After Telegram, включая 0) задаёт задержку точно (канон 04 §3.3).
    jitter - для тестов; в работе берётся случайный из [0.8, 1.2].
    """
    record = await repo.get(job_id, for_update=True)
    if record is None or record.status != "locked":
        raise ConflictError(FENCING_FAILED)
    text_error = error[:_ERROR_MAX_LENGTH]
    if record.attempts >= record.max_attempts:
        if not await repo.mark_dead(job_id, worker_id, text_error):
            raise ConflictError(FENCING_FAILED)
        return "dead"
    if retry_after_s is not None:
        delay = float(retry_after_s)
    else:
        factor = random.uniform(0.8, 1.2) if jitter is None else jitter
        delay = backoff_seconds(record.attempts, factor)
    if not await repo.mark_queued(job_id, worker_id, delay, text_error):
        raise ConflictError(FENCING_FAILED)
    return "queued"


async def unlock_expired(repo: JobRepo, now: datetime) -> list[JobRecord]:
    """Anti-stuck (канон 04 §1.3.14, ADR-28). Возвращает dead-строки для on-dead хуков (ТОП-2)."""
    return await repo.unlock_expired(now)
