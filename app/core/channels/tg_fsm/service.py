"""Use-case'ы FSM-state tg_worker: чтение, upsert, удаление, cleanup, каскад на /forget.

Ядро этого вопроса — persistent FSM вместо `MemoryStorage` aiogram (C-4). Правило 3
(headless-ядро): логика в core, HTTP-роутер — тонкий. Правило 4 (внешние сервисы через
порт): tg_worker ходит сюда HTTP под HMAC, прямого доступа к БД нет.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.core.channels.tg_fsm.repository import TgFsmStateRepo, TgFsmStateRow


async def get_state(repo: TgFsmStateRepo, key: str) -> TgFsmStateRow | None:
    """Строка FSM-state по ключу `aiogram`. Нет — None (BaseStorage вернёт пустые данные)."""
    return await repo.get(key)


async def set_state(
    repo: TgFsmStateRepo,
    key: str,
    state: str | None,
    data: Mapping[str, Any],
    now: datetime,
    user_id: int | None = None,
) -> None:
    """Полная замена state и data по ключу. `data` обязательно словарь (BaseStorage.set_data)."""
    await repo.upsert(key, state, data, now, user_id=user_id)


async def set_state_only(
    repo: TgFsmStateRepo,
    key: str,
    state: str | None,
    now: datetime,
    user_id: int | None = None,
) -> None:
    """Атомарный UPSERT одной колонки state (N-2 фазы 10b-3). Не трогает data."""
    await repo.set_state_only(key, state, now, user_id=user_id)


async def set_data_only(
    repo: TgFsmStateRepo,
    key: str,
    data: Mapping[str, Any],
    now: datetime,
    user_id: int | None = None,
) -> None:
    """Атомарный UPSERT одной колонки data (N-2 фазы 10b-3). Не трогает state."""
    await repo.set_data_only(key, data, now, user_id=user_id)


async def clear_state(repo: TgFsmStateRepo, key: str) -> None:
    """Удалить запись. Повтор безопасен: нет записи — тихое no-op."""
    await repo.delete(key)


async def cleanup_stale(
    repo: TgFsmStateRepo, now: datetime, stale_hours: int
) -> int:
    """Удалить строки, у которых `updated_at < now - stale_hours` (N-5 фазы 10b-3).

    Правило 5 (TTL в конфиге): `stale_hours` берётся из `app/config.py::fsm_stale_hours`.
    Возвращает rowcount — scheduler_tick пишет его в лог (без ПДн).
    """
    return await repo.cleanup_stale(now, stale_hours)
