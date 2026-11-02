"""Колесо момента (ADR-39, фаза 5): T1-T4 через core-дверь и настоящий Postgres.

Идём через core.activities.wheel.spin без моков БД. Призы и активность создаёт фабрика
make_activity/make_prize (сиды 0014 не меняем: stock_left остаётся между тестами).
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from app.common.errors import ConflictError
from app.core.activities.wheel import WheelSpinResult, pick_prize, spin
from app.core.activities.wheel.types import WheelPrizeRef
from app.core.types.ids import ChannelAccountId, UserId
from tests.factories import create_guest
from tests.factories.brand_d import (
    WHEEL_LIMITS,
    BrandDActivity,
    make_activity,
    make_prize,
    prize_stock,
    wheel_spin_rows,
)
from tests.factories.random import SeededRandomPort
from tests.factories.ugc import reward_rows

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)  # 12:00 по Москве, одна локальная дата
# Критерий плана: p-value >= 0.01 при df = 4 <=> chi2 <= 13.2767.
CHI2_CRITICAL_DF4_P01 = 13.2767


async def spin_as(
    uow_factory: Any,
    guest: Any,
    activity: BrandDActivity,
    rnd: SeededRandomPort,
    now: datetime = NOW,
) -> WheelSpinResult:
    async with uow_factory() as uow:
        repos = uow.repositories
        return await spin(
            repos.consents,
            repos.catalog,
            repos.attempts,
            repos.jobs,
            repos.rewards,
            repos.wheel,
            rnd,
            UserId(guest.user_id),
            activity.slug,
            ChannelAccountId(guest.channel_account_id),
            now,
        )


async def test_spin_without_consent_rejects(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T1: без действующего согласия spin отвечает consent_required и ничего не пишет."""
    guest = create_guest(sync_engine, external_id="3001", with_consent=False, now=NOW)
    activity = make_activity(sync_engine, "wheel", WHEEL_LIMITS)
    make_prize(sync_engine, activity.activity_id, "Комплимент стилиста", "compliment", 1)
    with pytest.raises(ConflictError) as info:
        await spin_as(uow_factory, guest, activity, SeededRandomPort(1))
    assert info.value.code == "consent_required"
    assert wheel_spin_rows(sync_engine, guest.user_id) == []


async def test_second_spin_same_day_rejects(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T2: второй спин того же гостя в ту же локальную дату - already_spun_today.

    Core-проверка wheel_spins идёт до start_attempt (U5): иначе сработал бы attempts_once_per_day.
    Уникальный индекс wheel_spins_once_per_day проверяется отдельно, в тесте БД ниже.
    """
    guest = create_guest(sync_engine, external_id="3002", now=NOW)
    activity = make_activity(sync_engine, "wheel", WHEEL_LIMITS)
    make_prize(sync_engine, activity.activity_id, "Комплимент стилиста", "compliment", 1)
    rnd = SeededRandomPort(2)
    await spin_as(uow_factory, guest, activity, rnd)
    with pytest.raises(ConflictError) as info:
        await spin_as(uow_factory, guest, activity, rnd, now=NOW + timedelta(hours=1))
    assert info.value.code == "already_spun_today"
    assert len(wheel_spin_rows(sync_engine, guest.user_id)) == 1


def test_spin_distribution_chi_square() -> None:
    """T3: 10000 выборов по весам 1/3/5/4/2 не расходятся с весами (chi-square, p >= 0.01)."""
    weights = [1, 3, 5, 4, 2]
    prizes = [
        WheelPrizeRef(
            id=index,
            activity_id=1,
            title=f"prize-{index}",
            prize_kind="mini_gift",
            weight=weight,
            stock_left=None,
            code_template=None,
        )
        for index, weight in enumerate(weights, start=1)
    ]
    rnd = SeededRandomPort(20261020)
    runs = 10000
    counts = Counter(pick_prize(prizes, rnd).id for _ in range(runs))
    total = sum(weights)
    chi2 = 0.0
    for prize in prizes:
        expected = runs * prize.weight / total
        chi2 += (counts[prize.id] - expected) ** 2 / expected
    assert chi2 <= CHI2_CRITICAL_DF4_P01, f"chi2={chi2:.3f} counts={dict(counts)}"


async def test_stock_depleted_rejects_or_skips(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T4: приз с остатком 1 выдан первому гостю; второй получает prizes_out_of_stock."""
    guest_a = create_guest(sync_engine, external_id="3004a", now=NOW)
    guest_b = create_guest(sync_engine, external_id="3004b", now=NOW)
    activity = make_activity(sync_engine, "wheel", WHEEL_LIMITS)
    prize_id = make_prize(sync_engine, activity.activity_id, "Пробник", "mini_gift", 1, 1)
    rnd = SeededRandomPort(4)
    await spin_as(uow_factory, guest_a, activity, rnd)
    assert prize_stock(sync_engine, prize_id) == 0
    with pytest.raises(ConflictError) as info:
        await spin_as(uow_factory, guest_b, activity, rnd)
    assert info.value.code == "prizes_out_of_stock"


async def test_promocode_prize_issues_code_and_mini_gift_issues_wheel_prize(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """B3: prize_kind='promocode' выдаёт reward promocode с кодом; mini_gift - wheel_prize без
    кода."""
    promo_guest = create_guest(sync_engine, external_id="3005a", now=NOW)
    mini_guest = create_guest(sync_engine, external_id="3005b", now=NOW)
    promo_activity = make_activity(sync_engine, "wheel", WHEEL_LIMITS)
    promo_prize = make_prize(
        sync_engine,
        promo_activity.activity_id,
        "Промокод -10%",
        "promocode",
        1,
        code_template="BRAND_D10-{random8}",
    )
    mini_activity = make_activity(sync_engine, "wheel", WHEEL_LIMITS)
    mini_prize = make_prize(sync_engine, mini_activity.activity_id, "Пробник", "mini_gift", 1)

    promo = await spin_as(uow_factory, promo_guest, promo_activity, SeededRandomPort(5))
    mini = await spin_as(uow_factory, mini_guest, mini_activity, SeededRandomPort(6))

    promo_rewards = reward_rows(sync_engine, promo.attempt_id)
    assert [row.reward_type for row in promo_rewards] == ["promocode"]
    assert re.fullmatch(r"BRAND_D10-[A-Z0-9]{8}", promo_rewards[0].code)
    assert promo_rewards[0].wheel_prize_id is None
    assert wheel_spin_rows(sync_engine, promo_guest.user_id)[0].wheel_prize_id == promo_prize

    mini_rewards = reward_rows(sync_engine, mini.attempt_id)
    assert [row.reward_type for row in mini_rewards] == ["wheel_prize"]
    assert mini_rewards[0].code is None
    assert mini_rewards[0].wheel_prize_id == mini_prize


async def test_prize_depleted_race_single_success(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """M-2: два гостя одновременно за последний приз - ровно один выигрыш, второй получает
    конфликт."""
    guest_a = create_guest(sync_engine, external_id="3007a", now=NOW)
    guest_b = create_guest(sync_engine, external_id="3007b", now=NOW)
    activity = make_activity(sync_engine, "wheel", WHEEL_LIMITS)
    prize_id = make_prize(sync_engine, activity.activity_id, "Пробник", "mini_gift", 1, 1)

    results = await asyncio.gather(
        spin_as(uow_factory, guest_a, activity, SeededRandomPort(8)),
        spin_as(uow_factory, guest_b, activity, SeededRandomPort(9)),
        return_exceptions=True,
    )

    successes = [item for item in results if not isinstance(item, BaseException)]
    failures = [item for item in results if isinstance(item, ConflictError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code in ("prize_depleted_race", "prizes_out_of_stock")
    assert prize_stock(sync_engine, prize_id) == 0
