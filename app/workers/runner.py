"""WorkerRunner (ADR-28): claim SKIP LOCKED, затем обработчик и ack - в одной транзакции на задачу.

Исход задачи:
- обычный выход обработчика -> done;
- TerminalBusinessError -> done с маркером terminal_business_error:<slug>, без повторов;
- RetryableJobError или любое исключение -> queued с backoff, либо dead. При dead зовётся
  on-dead хук вида kind в той же транзакции (ingest_ugc_file -> ingest_failed, канон 04 §4.6).
Каждый ack - с fencing по worker_id (ТОП-3): чужой замок не перезаписывается.
visibility_timeout_s - по виду: ingest_ugc_file 300 с (httpx 60 с + S3), остальные 90 с (ТОП-3).
Остановка по событию (SIGTERM/SIGINT): новые пачки не берутся, текущая доделывается.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from app.core.jobs import ack_retryable, ack_success, ack_terminal_business, claim_batch
from app.core.jobs.types import JobRecord, RetryableJobError, TerminalBusinessError
from app.db.unit_of_work import UnitOfWork
from app.workers.context import Handler, OnDead, WorkerDeps
from app.workers.handlers import HANDLERS, KIND_ON_DEAD

logger = logging.getLogger("app.workers.runner")
UowFactory = Callable[[], UnitOfWork]
DEFAULT_VISIBILITY_S: Final[int] = 90
INGEST_VISIBILITY_S: Final[int] = 300
DEFAULT_VISIBILITY_BY_KIND: Final[Mapping[str, int]] = {"ingest_ugc_file": INGEST_VISIBILITY_S}


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Ждать `seconds`, но выйти раньше, если поставлено событие остановки."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return


class WorkerRunner:
    def __init__(
        self,
        uow_factory: UowFactory,
        deps: WorkerDeps,
        *,
        kinds: Sequence[str] | None = None,
        worker_id: str | None = None,
        pool_size: int = 4,
        poll_interval_s: float = 1.0,
        visibility_default_s: int = DEFAULT_VISIBILITY_S,
        visibility_by_kind: Mapping[str, int] | None = None,
        handlers: Mapping[str, Handler] = HANDLERS,
        on_dead: Mapping[str, OnDead] = KIND_ON_DEAD,
    ) -> None:
        self._uow_factory = uow_factory
        self._deps = deps
        self._kinds = tuple(kinds) if kinds is not None else tuple(handlers)
        self._worker_id = worker_id or default_worker_id()
        self._pool_size = pool_size
        self._poll_interval_s = poll_interval_s
        self._visibility_default_s = visibility_default_s
        self._visibility_by_kind = dict(
            DEFAULT_VISIBILITY_BY_KIND if visibility_by_kind is None else visibility_by_kind
        )
        self._handlers = handlers
        self._on_dead = on_dead

    def _visibility_for(self, kind: str) -> int:
        return self._visibility_by_kind.get(kind, self._visibility_default_s)

    async def claim(self) -> list[JobRecord]:
        """Одна транзакция claim: виды группируются по visibility, пачка не больше pool_size."""
        groups: dict[int, list[str]] = {}
        for kind in self._kinds:
            groups.setdefault(self._visibility_for(kind), []).append(kind)
        claimed: list[JobRecord] = []
        async with self._uow_factory() as uow:
            for visibility, kinds in groups.items():
                room = self._pool_size - len(claimed)
                if room <= 0:
                    break
                claimed.extend(
                    await claim_batch(
                        uow.repositories.jobs,
                        self._worker_id,
                        kinds,
                        room,
                        visibility,
                    )
                )
        return claimed

    async def run_once(self) -> int:
        """Одна пачка: claim (коммит), затем параллельная обработка. Возвращает число задач."""
        jobs = await self.claim()
        if not jobs:
            return 0
        outcomes = await asyncio.gather(
            *(self._process(job) for job in jobs), return_exceptions=True
        )
        for job, outcome in zip(jobs, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.error(
                    "worker_job_failed",
                    extra={
                        "event": "worker_job_failed",
                        "job_id": int(job.id),
                        "kind": job.kind,
                        "error": type(outcome).__name__,
                    },
                )
        return len(jobs)

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except Exception as exc:
                logger.error(
                    "worker_loop_failed",
                    extra={"event": "worker_loop_failed", "error": type(exc).__name__},
                )
                processed = 0
            if processed == 0:
                await sleep_or_stop(stop, self._poll_interval_s)

    async def _process(self, job: JobRecord) -> None:
        handler = self._handlers[job.kind]
        async with self._uow_factory() as uow:
            try:
                await handler(job, uow, self._deps)
            except TerminalBusinessError as exc:
                # Бизнес-записи обработчика фиксируются вместе с маркером.
                await ack_terminal_business(
                    uow.repositories.jobs, job.id, self._worker_id, exc.slug
                )
                return
            except RetryableJobError as exc:
                await uow.session.rollback()
                await self._retry(uow, job, exc.error, exc.retry_after_s)
                return
            except Exception as exc:
                # Любой неожиданный сбой - временный, пока не исчерпаны попытки (канон 04 §3.3).
                await uow.session.rollback()
                await self._retry(uow, job, type(exc).__name__, None)
                return
            await ack_success(uow.repositories.jobs, job.id, self._worker_id)

    async def _retry(
        self,
        uow: UnitOfWork,
        job: JobRecord,
        error: str,
        retry_after_s: int | None,
    ) -> None:
        outcome = await ack_retryable(
            uow.repositories.jobs, job.id, self._worker_id, error, retry_after_s
        )
        logger.info(
            "job_retry",
            extra={
                "event": "job_retry",
                "job_id": int(job.id),
                "kind": job.kind,
                "outcome": outcome,
                "error": error,
            },
        )
        if outcome == "dead":
            hook = self._on_dead.get(job.kind)
            if hook is not None:
                await hook(job, uow, self._deps)
