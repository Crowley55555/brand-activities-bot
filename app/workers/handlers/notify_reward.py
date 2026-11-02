"""Handler notify_reward (ADR-31): награды прохождения -> telegram_send(reward_notice).

Текст и код здесь не собираются (outbox-паттерн): в payload задачи только id наград (п.8 ревью).
Рендер делает claim /outbox/claim. Все неотозванные награды прохождения идут в одно сообщение.
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
    voucher = await repos.rewards.get(reward_id)
    if voucher is None:
        raise TerminalBusinessError("reward_not_found")
    attempt = await get_attempt(repos.attempts, voucher.attempt_id)
    channel = await repos.channels.find_active_by_user(attempt.user_id, "telegram")
    if channel is None:
        raise TerminalBusinessError("no_telegram_channel")
    issued = await repos.rewards.list_for_attempt(voucher.attempt_id)
    reward_ids = [int(item.id) for item in issued if item.revoked_at is None]
    await enqueue(
        repos.jobs,
        "telegram_send",
        {
            "chat_id": telegram_chat_id(channel.external_id),
            "template": "reward_notice",
            "context": {"reward_ids": reward_ids},
        },
        idempotency_key=f"reward_notice:{int(reward_id)}",
    )
