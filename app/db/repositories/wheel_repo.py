"""SQL-реализация порта колеса (ADR-39): призы, спины и остаток. Дубль и гонку ловит БД, не код.

Спин - INSERT ... ON CONFLICT по уникальному индексу wheel_spins_once_per_day (None - спин уже
есть).
Остаток - UPDATE с предикатом stock_left > 0 или NULL (False - приз уже исчерпан в гонке).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.activities.wheel.types import WheelPrizeRef
from app.core.types.ids import ActivityId, AttemptId, UserId

_PRIZE_COLUMNS = "id, activity_id, title, prize_kind, weight, stock_left, code_template"


def _prize_ref(row: Any) -> WheelPrizeRef:
    return WheelPrizeRef(
        id=int(row.id),
        activity_id=int(row.activity_id),
        title=str(row.title),
        prize_kind=str(row.prize_kind),
        weight=int(row.weight),
        stock_left=None if row.stock_left is None else int(row.stock_left),
        code_template=None if row.code_template is None else str(row.code_template),
    )


class SqlWheelRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_available_prizes(self, activity_id: ActivityId) -> list[WheelPrizeRef]:
        rows = (
            await self._session.execute(
                text(
                    f"SELECT {_PRIZE_COLUMNS} FROM wheel_prizes "
                    "WHERE activity_id = :aid AND is_active AND deleted_at IS NULL "
                    "AND (stock_left IS NULL OR stock_left > 0) ORDER BY id"
                ),
                {"aid": int(activity_id)},
            )
        ).all()
        return [_prize_ref(row) for row in rows]

    async def has_spin_on(self, user_id: UserId, activity_id: ActivityId, spin_date: date) -> bool:
        result = await self._session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM wheel_spins "
                "WHERE user_id = :uid AND activity_id = :aid AND spin_date = :d)"
            ),
            {"uid": int(user_id), "aid": int(activity_id), "d": spin_date},
        )
        return bool(result.scalar_one())

    async def insert_spin(
        self,
        attempt_id: AttemptId,
        prize_id: int,
        user_id: UserId,
        activity_id: ActivityId,
        spun_at: datetime,
        spin_date: date,
    ) -> int | None:
        row = (
            await self._session.execute(
                text(
                    "INSERT INTO wheel_spins (attempt_id, wheel_prize_id, user_id, activity_id, "
                    "spun_at, spin_date, created_at, updated_at) "
                    "VALUES (:att, :prize, :uid, :aid, :ts, :d, :ts, :ts) "
                    "ON CONFLICT (user_id, activity_id, spin_date) DO NOTHING RETURNING id"
                ),
                {
                    "att": int(attempt_id),
                    "prize": prize_id,
                    "uid": int(user_id),
                    "aid": int(activity_id),
                    "ts": spun_at,
                    "d": spin_date,
                },
            )
        ).one_or_none()
        return None if row is None else int(row.id)

    async def decrement_stock(self, prize_id: int) -> bool:
        row = (
            await self._session.execute(
                text(
                    "UPDATE wheel_prizes SET stock_left = stock_left - 1, updated_at = now() "
                    "WHERE id = :id AND (stock_left IS NULL OR stock_left > 0) RETURNING id"
                ),
                {"id": prize_id},
            )
        ).one_or_none()
        return row is not None
