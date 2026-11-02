from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.core.jobs.types import JobKind, JobRecord
from app.core.types.ids import JobId


@dataclass(frozen=True)
class DeadJobRow:
    """Строка dead-задачи для DLQ-UI (фаза 10b-2, I-1): безопасные поля без payload.

    Канон 04 §3.1 + правило 2: сырой payload не выставляется наружу, у `telegram_send`
    там `chat_id`, у ingest — `telegram_file_id`. Для возврата к гостю достаточно
    `kind`+`last_error`; детальный просмотр payload — отдельная задача на будущее.
    """

    id: JobId
    kind: str
    attempts: int
    max_attempts: int
    last_error: str | None
    updated_at: datetime
    idempotency_key: str | None


@runtime_checkable
class JobRepo(Protocol):
    async def insert(
        self,
        kind: JobKind,
        payload: Mapping[str, Any],
        run_after: datetime | None,
        idempotency_key: str | None,
    ) -> JobId | None:
        """INSERT ... ON CONFLICT DO NOTHING RETURNING id. None - конфликт idempotency_key."""
        ...

    async def id_by_idempotency_key(self, idempotency_key: str) -> JobId | None: ...

    async def claim(
        self,
        worker_id: str,
        kinds: Sequence[str],
        limit: int,
        visibility_timeout_s: int,
    ) -> list[JobRecord]:
        """SELECT ... FOR UPDATE SKIP LOCKED и UPDATE: status=locked, locked_by, attempts+1."""
        ...

    async def get(self, job_id: JobId, *, for_update: bool = False) -> JobRecord | None: ...

    async def mark_done(self, job_id: JobId, worker_id: str, last_error: str | None) -> bool:
        """status=done, только если задача locked именно этим воркером (fencing, ТОП-3)."""
        ...

    async def mark_queued(
        self, job_id: JobId, worker_id: str, delay_s: float, last_error: str
    ) -> bool:
        """status=queued, run_after=now()+delay_s, только при locked_by=worker_id."""
        ...

    async def mark_dead(self, job_id: JobId, worker_id: str, last_error: str) -> bool:
        """status=dead (карантин), только при locked_by=worker_id."""
        ...

    async def unlock_expired(self, now: datetime) -> list[JobRecord]:
        """Истёкшие блокировки: queued, а исчерпавшие попытки - dead. Возвращает dead-строки.

        Dead-строки нужны on-dead хукам (ТОП-2): хук запускает планировщик в той же транзакции.
        """
        ...

    # ----- DLQ (фаза 10b-2, I-1) ---------------------------------------------

    async def list_dead(
        self, kind: str | None, limit: int, offset: int
    ) -> list[DeadJobRow]:
        """dead-задачи, фильтр по kind опциональный, сортировка по updated_at DESC."""
        ...

    async def count_dead(self, kind: str | None) -> int:
        """Количество dead-задач с учётом фильтра по kind (для пагинации)."""
        ...

    async def revive_dead(self, job_id: JobId, now: datetime) -> bool:
        """status: dead → queued атомарно; attempts=0, locked_*=NULL, last_error=NULL.

        Возвращает True, если задача была dead. False — если кто-то другой уже
        починил/похоронил её; админ-слой превращает это в ConflictError('not_dead').
        """
        ...

    async def bury_dead(self, job_id: JobId, last_error: str) -> bool:
        """status: dead → done атомарно, last_error = <маркер>. True если rowcount=1."""
        ...
