"""Виды задач и типы очереди (ADR-18, ADR-26, канон 04 §3.1).

Статусы - канон `queued|locked|done|failed|dead` (миграция 0006). ADR-27 отменён: миграция 0011
не нужна. Бизнес-отказ закрывает задачу как `done` с маркером `terminal_business_error:<slug>`
в `last_error` (канон 04 §3.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args

from app.core.types.ids import JobId

JobKind = Literal[
    "telegram_send",
    "polaroid_render",
    "ingest_ugc_file",
    "s3_delete",
    "cleanup_ugc_files",
    "notify_reward",
    "notify_ugc_rejected",
    "notify_ingest_failed",
    "notify_appointment_reminder",
    "auto_cancel_appointment",
    "notify_wheel_result",
    "notify_polaroid_ready",
    "notify_promocode",
    "notify_confirmation_rejected",
]
JobStatus = Literal["queued", "locked", "done", "failed", "dead"]
OutboxResult = Literal["success", "retryable", "terminal_business", "dead"]

JOB_KINDS: Final[frozenset[str]] = frozenset(get_args(JobKind))
TERMINAL_MARKER_PREFIX: Final[str] = "terminal_business_error:"


@dataclass(frozen=True)
class JobRecord:
    """Строка jobs в том виде, в каком её отдают claim и get (служебные поля блокировки опущены)."""

    id: JobId
    kind: str
    payload: Mapping[str, Any]
    status: str
    attempts: int
    max_attempts: int
    idempotency_key: str | None
    last_error: str | None


class RetryableJobError(Exception):
    """Временный сбой: задача вернётся в очередь с backoff (или ровно через retry_after_s)."""

    def __init__(self, error: str, retry_after_s: int | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.retry_after_s = retry_after_s


class TerminalBusinessError(Exception):
    """Бизнес-отказ: задача закрывается как done с маркером, без повторов и без dead."""

    def __init__(self, slug: str) -> None:
        super().__init__(slug)
        self.slug = slug
