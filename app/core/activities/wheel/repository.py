"""Порт колеса момента (ADR-39): призы, спины и остаток. SQL - app/db/repositories/wheel_repo.py."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from app.core.activities.wheel.types import WheelPrizeRef
from app.core.types.ids import ActivityId, AttemptId, UserId


@runtime_checkable
class WheelRepo(Protocol):
    async def list_available_prizes(self, activity_id: ActivityId) -> list[WheelPrizeRef]:
        """Активные неудалённые призы с остатком (NULL - без лимита), по возрастанию id."""
        ...

    async def has_spin_on(self, user_id: UserId, activity_id: ActivityId, spin_date: date) -> bool:
        """Есть ли спин гостя по активности за локальную дату (core-check, U5)."""
        ...

    async def insert_spin(
        self,
        attempt_id: AttemptId,
        prize_id: int,
        user_id: UserId,
        activity_id: ActivityId,
        spun_at: datetime,
        spin_date: date,
    ) -> int | None:
        """INSERT wheel_spins ... ON CONFLICT (user, activity, date) DO NOTHING. None - дубль."""
        ...

    async def decrement_stock(self, prize_id: int) -> bool:
        """UPDATE stock_left - 1 WHERE остаток > 0 или NULL. False - остаток уже исчерпан."""
        ...
