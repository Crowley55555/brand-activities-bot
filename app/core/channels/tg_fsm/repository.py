"""Порт TgFsmStateRepo: атомарные операции над таблицей `tg_fsm_state` (миграция 0018 + 0021)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class TgFsmStateRow:
    """Строка tg_fsm_state. `state` None — состояние не задано (стартовый экран FSM)."""

    key: str
    state: str | None
    data: Mapping[str, Any]
    updated_at: datetime
    user_id: int | None = None


@runtime_checkable
class TgFsmStateRepo(Protocol):
    async def get(self, key: str) -> TgFsmStateRow | None:
        """Запись по ключу (всю: state + data). Нет записи — None."""
        ...

    async def upsert(
        self,
        key: str,
        state: str | None,
        data: Mapping[str, Any],
        now: datetime,
        user_id: int | None = None,
    ) -> None:
        """INSERT ... ON CONFLICT (key) DO UPDATE: заменяем state и data целиком.

        Канон 0018: `data JSONB NOT NULL DEFAULT '{}'` — пустое значение валидно.
        Миграция 0021 (фаза 10b-3, I-2): опциональный `user_id` для /forget каскада.
        """
        ...

    async def delete(self, key: str) -> None:
        """Удалить запись. Нет записи — тихое no-op (повтор idempotent)."""
        ...

    async def set_state_only(
        self,
        key: str,
        state: str | None,
        now: datetime,
        user_id: int | None = None,
    ) -> None:
        """Атомарный UPSERT одной колонки `state` (N-2 фазы 10b-3).

        INSERT с `data='{}'::jsonb` на новую строку, ON CONFLICT DO UPDATE
        не трогает `data`. tg_worker storage.set_state больше не делает
        read-modify-write (между get и set другая реплика могла перезаписать data).
        """
        ...

    async def set_data_only(
        self,
        key: str,
        data: Mapping[str, Any],
        now: datetime,
        user_id: int | None = None,
    ) -> None:
        """Атомарный UPSERT одной колонки `data` (N-2 фазы 10b-3).

        INSERT с `state=NULL` на новую строку, ON CONFLICT DO UPDATE
        не трогает `state`. Симметрично `set_state_only`.
        """
        ...

    async def delete_by_user(self, user_id: int) -> int:
        """Удалить все строки конкретного пользователя (/forget каскад I-2).

        Возвращает число удалённых строк для аудит-лога. Строки без user_id
        (исторические, до миграции 0021) не трогает — их чистит cleanup_stale.
        """
        ...

    async def cleanup_stale(self, now: datetime, stale_hours: int) -> int:
        """Удалить строки с `updated_at < now - stale_hours` (N-5 фазы 10b-3).

        Возвращает число удалённых. Защита от висячих FSM-данных регистрации,
        которые гость не завершил и не нажал /forget.
        """
        ...
