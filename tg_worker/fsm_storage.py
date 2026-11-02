"""HttpFsmStorage: aiogram BaseStorage поверх InternalApiClient (C-4, фаза 10a).

Канон: tg_worker ходит в ядро только по HTTP под HMAC (правило 4). FSM-состояние живёт в
основной БД (таблица `tg_fsm_state`, миграция 0018). Этот storage превращает ключи aiogram
(StorageKey) в строковые ключи БД и делегирует get/set/clear ядру.

Рестарт процесса сохраняет состояние — вторая реплика tg_worker видит те же данные.
Без storage на рестарт `MemoryStorage` терял FSM, и гость, которого бот спрашивает
«введите фамилию», отвечал в пустоту.

Фаза 10b-3:
- N-2: `set_state`/`set_data` зовут атомарные `/set_state_only` и `/set_data_only`
  вместо read-modify-write — две параллельные реплики не затирают друг друга;
- N-3: `FsmHttpError` ловится здесь — чтение fail-open (None/{}), запись fail-closed
  (исключение пробрасывается, aiogram покажет гостю «попробуйте ещё раз»).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from tg_worker.internal_client.httpx_json import FsmHttpError, InternalApiClient

logger = logging.getLogger("tg_worker.fsm_storage")


def _format_key(key: StorageKey) -> str:
    """Из StorageKey -> стабильная строка `bot:chat:user:thread:bconn:destiny`.

    Включает все поля (включая None и destiny), чтобы разные контексты не схлопнулись.
    """
    return (
        f"{key.bot_id}:{key.chat_id}:{key.user_id}:"
        f"{'' if key.thread_id is None else key.thread_id}:"
        f"{'' if key.business_connection_id is None else key.business_connection_id}:"
        f"{key.destiny}"
    )


def _state_as_string(state: StateType) -> str | None:
    if state is None:
        return None
    if isinstance(state, State):
        return state.state
    return str(state)


class HttpFsmStorage(BaseStorage):
    """Реализация aiogram BaseStorage поверх ядрового HTTP API под HMAC.

    Клиент передаётся снаружи, его жизненный цикл — у вызывающего (bot.run_polling).
    `close` ничего не делает: клиент закрывает run_polling сам (как в фазе 3c).
    """

    def __init__(self, client: InternalApiClient) -> None:
        self._client = client

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        """N-2 (фаза 10b-3): атомарный UPSERT state через `/set_state_only`.

        Прежний read-modify-write между `fsm_get` и `fsm_set` при двух репликах
        затирал data: репа A кладёт `data={a:1}`, репа B читает `data={}` до
        неё, пишет `state=X, data={}` — поле `a` потеряно. Теперь ядро
        обновляет только `state` в UPSERT, data остаётся прежней.
        """
        skey = _format_key(key)
        state_str = _state_as_string(state)
        try:
            await self._client.fsm_set_state(skey, state_str)
        except FsmHttpError:
            # Fail-closed на запись: aiogram-диспетчер поймает ошибку и покажет
            # гостю общий текст (регистрация прервётся, но не продолжится молча).
            raise

    async def get_state(self, key: StorageKey) -> str | None:
        """N-3 (фаза 10b-3): fail-open на чтение — 5xx возвращает None.

        aiogram на None считает, что состояние не задано (стартовый экран).
        Следующий update затрёт state корректно, если ядро поднимется.
        """
        try:
            state, _ = await self._client.fsm_get(_format_key(key))
        except FsmHttpError:
            return None
        return state

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        """N-2 (фаза 10b-3): атомарный UPSERT data через `/set_data_only`."""
        skey = _format_key(key)
        try:
            await self._client.fsm_set_data(skey, dict(data))
        except FsmHttpError:
            raise

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        """N-3 (фаза 10b-3): fail-open на чтение — 5xx возвращает {}."""
        try:
            _, data = await self._client.fsm_get(_format_key(key))
        except FsmHttpError:
            return {}
        return dict(data)

    async def close(self) -> None:
        # Клиентом владеет вызывающий (run_polling): здесь не закрываем.
        return None
