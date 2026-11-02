"""Тик планировщика (ADR-28, фаза 10a):

Раз в минуту снимаем истёкшие блокировки, запускаем on-dead хуки, затем:
- `expire_stale_attempts` — закрывает забытые in_progress в `expired` (C-2);
- `expire_pending_moderation` — закрывает пропущенные pending_moderation в `cancelled`
  с системным уведомлением гостю (C-3);
- `run_due_scans` — booking-напоминания и авто-отмена (фаза 4);
- `cleanup_stale_fsm` — удаляет протухшие строки tg_fsm_state (N-5 фазы 10b-3).

Каждый блок — отдельная транзакция (M1 ревью фазы 4): сбой одного не откатывает другого.

Фаза 10b-3, N-1: `limit` джобов `expire_*` берётся из `EXPIRE_BATCH_SIZE` (config).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Final

from app.config import get_settings
from app.core.activities.booking import run_due_scans
from app.core.attempts import expire_pending_moderation, expire_stale_attempts
from app.core.channels.tg_fsm import cleanup_stale as cleanup_stale_fsm
from app.core.jobs import unlock_expired
from app.core.types.ids import JobId
from app.db.unit_of_work import UnitOfWork
from app.workers.context import OnDead, WorkerDeps
from app.workers.handlers import KIND_ON_DEAD
from app.workers.runner import sleep_or_stop

logger = logging.getLogger("app.workers.scheduler")
TICK_INTERVAL_S: Final[float] = 60.0


async def scheduler_tick(
    uow_factory: Callable[[], UnitOfWork],
    now: datetime,
    deps: WorkerDeps,
    on_dead: Mapping[str, OnDead] = KIND_ON_DEAD,
) -> list[JobId]:
    """Снимает истёкшие блокировки, запускает expire-джобы, сканеры booking и чистильщик FSM.

    Все блоки — независимые транзакции: сбой одного не откатывает другие. Правило 2
    (ПДн в логах): extra содержит только event и count, user-данные не логируются.
    """
    settings = get_settings()
    batch = settings.expire_batch_size
    async with uow_factory() as uow:
        dead = await unlock_expired(uow.repositories.jobs, now)
        for record in dead:
            hook = on_dead.get(record.kind)
            if hook is not None:
                await hook(record, uow, deps)
    # C-2 (фаза 10a): expire_stale_attempts. Отдельная транзакция: сбой не роняет остальных.
    try:
        async with uow_factory() as uow:
            expired = await expire_stale_attempts(
                uow.repositories.attempts, now, limit=batch
            )
        if expired:
            logger.info(
                "attempts_expired",
                extra={"event": "attempts_expired", "count": expired},
            )
    except Exception as exc:
        logger.error(
            "attempts_expire_failed",
            extra={
                "event": "attempts_expire_failed",
                "error": type(exc).__name__,
            },
        )
    # C-3 (фаза 10a): expire_pending_moderation. Отдельная транзакция. Активности с TTL
    # читаем сами: список маленький (UGC их 3), ядро не ходит в код tg_worker.
    try:
        async with uow_factory() as uow:
            activities = await uow.repositories.catalog.list_activities_with_pending_ttl()
            cancelled = await expire_pending_moderation(
                uow.repositories.attempts,
                uow.repositories.catalog,
                uow.repositories.jobs,
                activities,
                now,
                limit=batch,
            )
        if cancelled:
            logger.info(
                "pending_moderation_expired",
                extra={"event": "pending_moderation_expired", "count": cancelled},
            )
    except Exception as exc:
        logger.error(
            "pending_moderation_expire_failed",
            extra={
                "event": "pending_moderation_expire_failed",
                "error": type(exc).__name__,
            },
        )
    try:
        async with uow_factory() as uow:
            await run_due_scans(uow.repositories, now)
    except Exception as exc:
        logger.error(
            "booking_scanner_failed",
            extra={"event": "booking_scanner_failed", "error": type(exc).__name__},
        )
    # N-5 (фаза 10b-3): чистильщик tg_fsm_state. Отдельная транзакция.
    try:
        async with uow_factory() as uow:
            removed = await cleanup_stale_fsm(
                uow.repositories.tg_fsm, now, settings.fsm_stale_hours
            )
        if removed:
            logger.info(
                "fsm_stale_removed",
                extra={"event": "fsm_stale_removed", "count": removed},
            )
    except Exception as exc:
        logger.error(
            "fsm_stale_cleanup_failed",
            extra={
                "event": "fsm_stale_cleanup_failed",
                "error": type(exc).__name__,
            },
        )
    return [record.id for record in dead]


async def run_scheduler(
    uow_factory: Callable[[], UnitOfWork],
    deps: WorkerDeps,
    stop: asyncio.Event,
    interval_s: float = TICK_INTERVAL_S,
) -> None:
    while not stop.is_set():
        try:
            released = await scheduler_tick(uow_factory, deps.clock.now(), deps)
            if released:
                logger.info(
                    "scheduler_dead_after_expiry",
                    extra={"event": "scheduler_dead_after_expiry", "count": len(released)},
                )
        except Exception as exc:
            logger.error(
                "scheduler_tick_failed",
                extra={"event": "scheduler_tick_failed", "error": type(exc).__name__},
            )
        await sleep_or_stop(stop, interval_s)
