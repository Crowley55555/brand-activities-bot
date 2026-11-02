"""Handler notify_ingest_failed (ТОП-1 ревью фазы 3c): системный сбой приёмки -> telegram_send.

Причина - системный слаг (activities.limits.system_reject_reasons, миграция 0011); текст и подпись
рендерит claim_outbox (шаблон ingest_failed). Согласие не проверяем: уведомление не является
записью ПДн (решение H ревью 3c). Код промокода здесь не нужен и в payload не попадает.
"""

from __future__ import annotations

from app.core.attempts import get_attempt
from app.core.jobs import enqueue
from app.core.jobs.types import JobRecord, TerminalBusinessError
from app.core.types.ids import AttemptId, UserId
from app.db.unit_of_work import UnitOfWork
from app.workers.context import WorkerDeps
from app.workers.handlers.common import telegram_chat_id


async def run(job: JobRecord, uow: UnitOfWork, deps: WorkerDeps) -> None:
    repos = uow.repositories
    user_id = UserId(int(job.payload["user_id"]))
    attempt_id = AttemptId(int(job.payload["attempt_id"]))
    channel = await repos.channels.find_active_by_user(user_id, "telegram")
    if channel is None:
        raise TerminalBusinessError("no_telegram_channel")
    attempt = await get_attempt(repos.attempts, attempt_id)
    key = job.idempotency_key or f"job:{int(job.id)}"
    await enqueue(
        repos.jobs,
        "telegram_send",
        {
            "chat_id": telegram_chat_id(channel.external_id),
            "template": "ingest_failed",
            "context": {
                "reason_slug": str(job.payload["system_reason_slug"]),
                "attempt_closed": bool(job.payload["attempt_closed"]),
                "activity_slug": attempt.activity_slug,
            },
        },
        idempotency_key=f"telegram_send:{key}",
    )
