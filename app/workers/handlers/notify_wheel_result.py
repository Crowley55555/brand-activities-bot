"""Handler notify_wheel_result (ADR-39): награда колеса -> telegram_send.

Промокод приза (reward_type='promocode') уходит шаблоном promocode (с кодом), остальные призы -
шаблоном
wheel_result. В payload задачи только reward_id: текст и код собирает claim_outbox, код в payload не
попадает (правило 9, журнал 3b).
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
    template = "promocode" if reward.reward_type == "promocode" else "wheel_result"
    await enqueue(
        repos.jobs,
        "telegram_send",
        {
            "chat_id": telegram_chat_id(channel.external_id),
            "template": template,
            "context": {"reward_ids": [int(reward_id)]},
        },
        idempotency_key=f"wheel_result:{int(reward_id)}",
    )
