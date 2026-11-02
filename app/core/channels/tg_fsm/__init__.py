"""FSM-state для tg_worker (C-4 pre-pilot).

Правило 4: tg_worker ходит в ядро только через HTTP под HMAC. Это ядро хранит FSM-состояние
регистрации (имя, фамилия, телефон, email) в основной БД, а не в памяти процесса aiogram.
"""

from __future__ import annotations

from app.core.channels.tg_fsm.repository import TgFsmStateRepo, TgFsmStateRow
from app.core.channels.tg_fsm.service import (
    cleanup_stale,
    clear_state,
    get_state,
    set_data_only,
    set_state,
    set_state_only,
)

__all__ = [
    "TgFsmStateRepo",
    "TgFsmStateRow",
    "cleanup_stale",
    "clear_state",
    "get_state",
    "set_data_only",
    "set_state",
    "set_state_only",
]
