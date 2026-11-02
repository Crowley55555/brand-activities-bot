"""SQL-реализация TgFsmStateRepo (миграция 0018, C-4; миграция 0021, фаза 10b-3)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channels.tg_fsm.repository import TgFsmStateRow


class SqlTgFsmStateRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> TgFsmStateRow | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT key, state, data, updated_at, user_id "
                    "FROM tg_fsm_state WHERE key = :k"
                ),
                {"k": key},
            )
        ).one_or_none()
        if row is None:
            return None
        return TgFsmStateRow(
            key=str(row.key),
            state=None if row.state is None else str(row.state),
            data=dict(row.data or {}),
            updated_at=row.updated_at,
            user_id=None if row.user_id is None else int(row.user_id),
        )

    async def upsert(
        self,
        key: str,
        state: str | None,
        data: Mapping[str, Any],
        now: datetime,
        user_id: int | None = None,
    ) -> None:
        # ON CONFLICT (key) DO UPDATE: безопасно для параллельного set на одном ключе.
        # data передаём как JSON-строку с CAST — mapping может содержать не-json dict-подклассы.
        # Фаза 10b-3: user_id сохраняем, если передан; при NULL старую запись не затираем.
        await self._session.execute(
            text(
                "INSERT INTO tg_fsm_state (key, state, data, updated_at, user_id) "
                "VALUES (:k, :s, CAST(:d AS jsonb), :ts, :uid) "
                "ON CONFLICT (key) DO UPDATE SET "
                "  state = EXCLUDED.state, "
                "  data = EXCLUDED.data, "
                "  updated_at = EXCLUDED.updated_at, "
                "  user_id = COALESCE(EXCLUDED.user_id, tg_fsm_state.user_id)"
            ),
            {
                "k": key,
                "s": state,
                "d": json.dumps(dict(data), ensure_ascii=False),
                "ts": now,
                "uid": user_id,
            },
        )

    async def delete(self, key: str) -> None:
        await self._session.execute(
            text("DELETE FROM tg_fsm_state WHERE key = :k"), {"k": key}
        )

    async def set_state_only(
        self,
        key: str,
        state: str | None,
        now: datetime,
        user_id: int | None = None,
    ) -> None:
        """Атомарный UPSERT: пишем только state, не трогаем data (N-2 фазы 10b-3).

        На новую строку кладём data='{}'::jsonb (default колонки). На существующей
        ON CONFLICT DO UPDATE обновляет state + updated_at, data остаётся прежней —
        tg_worker больше не делает read-modify-write.
        """
        await self._session.execute(
            text(
                "INSERT INTO tg_fsm_state (key, state, data, updated_at, user_id) "
                "VALUES (:k, :s, '{}'::jsonb, :ts, :uid) "
                "ON CONFLICT (key) DO UPDATE SET "
                "  state = EXCLUDED.state, "
                "  updated_at = EXCLUDED.updated_at, "
                "  user_id = COALESCE(EXCLUDED.user_id, tg_fsm_state.user_id)"
            ),
            {"k": key, "s": state, "ts": now, "uid": user_id},
        )

    async def set_data_only(
        self,
        key: str,
        data: Mapping[str, Any],
        now: datetime,
        user_id: int | None = None,
    ) -> None:
        """Атомарный UPSERT: пишем только data, не трогаем state (N-2 фазы 10b-3).

        На новую строку state=NULL (стартовый экран). На существующей ON CONFLICT
        DO UPDATE обновляет data + updated_at.
        """
        await self._session.execute(
            text(
                "INSERT INTO tg_fsm_state (key, state, data, updated_at, user_id) "
                "VALUES (:k, NULL, CAST(:d AS jsonb), :ts, :uid) "
                "ON CONFLICT (key) DO UPDATE SET "
                "  data = EXCLUDED.data, "
                "  updated_at = EXCLUDED.updated_at, "
                "  user_id = COALESCE(EXCLUDED.user_id, tg_fsm_state.user_id)"
            ),
            {
                "k": key,
                "d": json.dumps(dict(data), ensure_ascii=False),
                "ts": now,
                "uid": user_id,
            },
        )

    async def delete_by_user(self, user_id: int) -> int:
        """DELETE WHERE user_id = :uid — каскад /forget (I-2 фазы 10b-3)."""
        result = await self._session.execute(
            text("DELETE FROM tg_fsm_state WHERE user_id = :uid"),
            {"uid": int(user_id)},
        )
        return int(result.rowcount or 0)

    async def cleanup_stale(self, now: datetime, stale_hours: int) -> int:
        """DELETE WHERE updated_at < now - stale_hours — чистильщик (N-5 фазы 10b-3)."""
        result = await self._session.execute(
            text(
                "DELETE FROM tg_fsm_state "
                "WHERE updated_at < :ts - make_interval(hours => :hours)"
            ),
            {"ts": now, "hours": int(stale_hours)},
        )
        return int(result.rowcount or 0)
