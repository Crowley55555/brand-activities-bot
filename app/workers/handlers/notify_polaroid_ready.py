"""Handler notify_polaroid_ready (ADR-40, B4): результат Polaroid -> telegram_send (шаблон
polaroid_ready).

chat_id и frame_submission_id ставит polaroid_render. Текст со ссылкой на /media/frame/<id> собирает
claim_outbox: ссылка подписана и короткоживущая, в payload задачи её нет.
"""

from __future__ import annotations

from app.core.jobs import enqueue
from app.core.jobs.types import JobRecord
from app.db.unit_of_work import UnitOfWork
from app.workers.context import WorkerDeps


async def run(job: JobRecord, uow: UnitOfWork, deps: WorkerDeps) -> None:
    repos = uow.repositories
    frame_id = int(job.payload["frame_submission_id"])
    key = job.idempotency_key or f"job:{int(job.id)}"
    await enqueue(
        repos.jobs,
        "telegram_send",
        {
            "chat_id": int(job.payload["chat_id"]),
            "template": "polaroid_ready",
            "context": {"frame_submission_id": frame_id},
        },
        idempotency_key=f"telegram_send:{key}",
    )
