"""Фаза 7b - use-case list_rewards_page: фильтры типа и статуса."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.rewards import list_rewards_page
from app.db.unit_of_work import UnitOfWork
from tests.factories import create_attempt, create_guest

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def _reward(engine: Engine, n: int, reward_type: str, *, redeemed: bool = False) -> None:
    code = f"C-{n}" if reward_type in ("promocode", "voucher") else None
    guest = create_guest(engine, f"lr-{n}")
    attempt_id = create_attempt(engine, guest, "brand_a_skin_survey")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rewards (attempt_id, reward_type, code, issued_at, redeemed_at, "
                "wheel_prize_id, created_at, updated_at) VALUES (:a, :t, :c, :ts, "
                "CASE WHEN :red THEN CAST(:ts AS timestamptz) END, "
                "CASE WHEN :t = 'wheel_prize' THEN (SELECT min(id) FROM wheel_prizes) END, "
                ":ts, :ts)"
            ),
            {"a": attempt_id, "t": reward_type, "c": code, "ts": NOW, "red": redeemed},
        )


async def test_list_rewards_all_returns_only_promocode_and_voucher(
    clean_db: None, sync_engine: Engine, uow_factory: Callable[[], UnitOfWork]
) -> None:
    for n, kind in enumerate(["promocode", "voucher", "wheel_prize", "leaflet"]):
        _reward(sync_engine, n, kind)

    async with uow_factory() as uow:
        rows, total = await list_rewards_page(uow.repositories.rewards, "all", "all", None, 50, 0)

    assert {r.reward_type for r in rows} == {"promocode", "voucher"}
    assert total == 2


async def test_list_rewards_status_filters_correctly(
    clean_db: None, sync_engine: Engine, uow_factory: Callable[[], UnitOfWork]
) -> None:
    _reward(sync_engine, 1, "promocode")
    _reward(sync_engine, 2, "promocode", redeemed=True)

    async with uow_factory() as uow:
        r = uow.repositories.rewards
        issued, n_issued = await list_rewards_page(r, "promocode", "issued", None, 50, 0)
        redeemed, n_red = await list_rewards_page(r, "promocode", "redeemed", None, 50, 0)

    assert [x.code for x in issued] == ["C-1"]
    assert n_issued == 1
    assert [x.code for x in redeemed] == ["C-2"]
    assert n_red == 1
