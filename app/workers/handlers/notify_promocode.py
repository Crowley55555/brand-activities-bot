"""Handler notify_promocode (ADR-42): промокод подарка за пост -> telegram_send (шаблон promocode).

В payload только reward_id; код собирает claim_outbox из reward и не кладёт его в payload задачи.
"""

from __future__ import annotations

from app.core.attempts import get_attempt
from app.core.jobs import enqueue
from app.core.jobs.types import JobRecord, TerminalBusinessError
from app.core.types.ids import RewardId
from app.db.unit_of_work import UnitOfWork
from app.workers.context import WorkerDeps
from app.workers.handlers.common import telegram_chat_id


async def run(job: JobRecord, uow: UnitOfWork, deps: WorkerDeps) -> None:
    repos = uow.repositories
    reward_id = RewardId(int(job.payload["reward_id"]))
    reward = await repos.rewards.get(reward_id)
    if reward is None:
        raise TerminalBusinessError("reward_not_found")
    attempt = await get_attempt(repos.attempts, reward.attempt_id)
    channel = await repos.channels.find_active_by_user(attempt.user_id, "telegram")
    if channel is None:
        raise TerminalBusinessError("no_telegram_channel")
    await enqueue(
        repos.jobs,
        "telegram_send",
        {
            "chat_id": telegram_chat_id(channel.external_id),
            "template": "promocode",
            "context": {"reward_ids": [int(reward_id)]},
        },
        idempotency_key=f"promocode_notice:{int(reward_id)}",
    )
