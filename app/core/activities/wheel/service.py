"""Колесо момента (ADR-39, фаза 5): взвешенный выбор приза и один спин в день.

Правило 1: согласие - первой строкой spin. Порядок в одной транзакции (U5, B3):
consent -> активность kind='wheel' -> core-check спина за локальную дату (already_spun_today) ->
start_attempt -> призы с остатком -> random.choice -> INSERT wheel_spins (уникальный индекс БД) ->
UPDATE остатка -> close_attempt(completed) -> grant (promocode с кодом или wheel_prize) ->
enqueue notify_wheel_result. Ошибка любого шага откатывает спин и остаток целиком.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final

from app.common.errors import ConflictError, ValidationError
from app.common.time_utils import attempt_date_for
from app.core.activities.wheel.repository import WheelRepo
from app.core.activities.wheel.types import WheelPrizeRef, WheelSpinResult
from app.core.attempts import AttemptRepo, close_attempt, start_attempt
from app.core.catalog import CatalogRepo, get_activity_by_slug
from app.core.consent import ConsentRepo, require_consent
from app.core.jobs import JobRepo, enqueue
from app.core.ports.random_source import RandomPort
from app.core.rewards import RewardRepo, grant_on_completion
from app.core.types.dto import GrantSpec
from app.core.types.ids import ChannelAccountId, UserId

ACTIVITY_KIND: Final = "wheel"
PROMOCODE_PRIZE_KIND: Final[str] = "promocode"


def pick_prize(prizes: Sequence[WheelPrizeRef], random: RandomPort) -> WheelPrizeRef:
    """Взвешенный выбор приза. Пустой список - призов с остатком нет (prizes_out_of_stock)."""
    if not prizes:
        raise ConflictError("prizes_out_of_stock")
    return random.choice(list(prizes), [prize.weight for prize in prizes])


def _grant_for(prize: WheelPrizeRef) -> GrantSpec:
    """B3: промокод - reward promocode с кодом по шаблону приза; остальное - wheel_prize без
    кода."""
    if prize.prize_kind == PROMOCODE_PRIZE_KIND:
        return GrantSpec(
            reward_type="promocode", title=prize.title, code_template=prize.code_template
        )
    return GrantSpec(reward_type="wheel_prize", title=prize.title, wheel_prize_id=prize.id)


async def spin(
    consents: ConsentRepo,
    catalog: CatalogRepo,
    attempts: AttemptRepo,
    jobs: JobRepo,
    rewards: RewardRepo,
    wheel: WheelRepo,
    random: RandomPort,
    user_id: UserId,
    activity_slug: str,
    channel_account_id: ChannelAccountId,
    now: datetime,
) -> WheelSpinResult:
    await require_consent(consents, user_id)
    activity = await get_activity_by_slug(catalog, activity_slug)
    if activity.kind != ACTIVITY_KIND:
        raise ValidationError("activity_kind_mismatch")
    spin_date = attempt_date_for(now)
    if await wheel.has_spin_on(user_id, activity.id, spin_date):
        raise ConflictError("already_spun_today")
    started = await start_attempt(
        consents,
        catalog,
        attempts,
        user_id,
        activity_slug,
        channel_account_id,
        now,
        expected_kind=ACTIVITY_KIND,
    )
    prize = pick_prize(await wheel.list_available_prizes(activity.id), random)
    spin_id = await wheel.insert_spin(
        started.attempt_id, prize.id, user_id, activity.id, now, spin_date
    )
    if spin_id is None:
        raise ConflictError("already_spun_today")
    if not await wheel.decrement_stock(prize.id):
        raise ConflictError("prize_depleted_race")
    await close_attempt(attempts, started.attempt_id, "completed", f"wheel:{prize.title}", now)
    issued = await grant_on_completion(rewards, started.attempt_id, [_grant_for(prize)], now)
    reward = issued[0]
    await enqueue(
        jobs,
        "notify_wheel_result",
        {"reward_id": int(reward.id)},
        run_after=now,
        idempotency_key=f"wheel_result:{int(started.attempt_id)}",
    )
    return WheelSpinResult(
        attempt_id=int(started.attempt_id),
        reward_id=int(reward.id),
        prize_title=prize.title,
        prize_kind=prize.prize_kind,
        code=reward.code,
    )
