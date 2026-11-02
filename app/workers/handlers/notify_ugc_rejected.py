"""Handler notify_ugc_rejected (ADR-31): отказ или сбой загрузки -> telegram_send(ugc_rejected).

В context попадают причина (слаг), признак закрытия прохождения и слаг активности. Подпись причины
берёт рендер из activities.limits.reject_reasons (миграция 0010), текст - из конфига.
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
            "template": "ugc_rejected",
            "context": {
                "reason_slug": str(job.payload["reason_slug"]),
                "attempt_closed": bool(job.payload["attempt_closed"]),
                "activity_slug": attempt.activity_slug,
            },
        },
        idempotency_key=f"telegram_send:{key}",
    )
