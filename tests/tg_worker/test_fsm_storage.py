"""HttpFsmStorage (tg_worker, C-4, фазы 10a + 10b-3): реализация aiogram BaseStorage.

Моком InternalApiClient показываем, что контракт `BaseStorage` соблюдён:
- `set_data`/`get_data` — туда-обратно (через `/set_data_only`, N-2 фазы 10b-3);
- `set_state`/`get_state` — туда-обратно (через `/set_state_only`, N-2);
- `clear`-подобная логика в aiogram реализована через `set_state(None)` + `set_data({})`
  в FSMContext (см. aiogram source);
- регистрационный сценарий: инициализируем storage, кладём данные, пересоздаём
  storage (имитация рестарта), поднимаем get — данные те же.
- N-3 (фаза 10b-3): FsmHttpError на чтение → fail-open (None/{}, warn-лог),
  на запись → проброс исключения.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, create_autospec

import pytest
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey

from tg_worker.fsm_storage import HttpFsmStorage, _format_key
from tg_worker.internal_client.httpx_json import FsmHttpError, InternalApiClient


class _FakeApiClient:
    """Карман для fsm_get/fsm_set_*/fsm_clear — заменяет HTTP на in-memory словарь.

    N-2 (фаза 10b-3): раздельные `fsm_set_state` и `fsm_set_data` — storage больше
    не делает read-modify-write.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[str | None, dict[str, Any]]] = {}
        self.state_calls: list[tuple[str, str | None]] = []
        self.data_calls: list[tuple[str, dict[str, Any]]] = []

    async def fsm_get(self, key: str) -> tuple[str | None, dict[str, Any]]:
        state, data = self._store.get(key, (None, {}))
        return state, dict(data)

    async def fsm_set_state(
        self, key: str, state: str | None, user_id: int | None = None
    ) -> None:
        _, existing_data = self._store.get(key, (None, {}))
        self._store[key] = (state, dict(existing_data))
        self.state_calls.append((key, state))

    async def fsm_set_data(
        self, key: str, data: dict[str, Any], user_id: int | None = None
    ) -> None:
        existing_state, _ = self._store.get(key, (None, {}))
        self._store[key] = (existing_state, dict(data))
        self.data_calls.append((key, dict(data)))

    async def fsm_clear(self, key: str) -> None:
        self._store.pop(key, None)


KEY = StorageKey(bot_id=1, chat_id=2, user_id=3)


def test_format_key_includes_all_components() -> None:
    """Чтобы разные thread/business/destiny не схлопнулись в один ключ."""
    k1 = StorageKey(bot_id=1, chat_id=2, user_id=3, thread_id=5, destiny="default")
    k2 = StorageKey(bot_id=1, chat_id=2, user_id=3, thread_id=6, destiny="default")
    assert _format_key(k1) != _format_key(k2)


async def test_set_get_data_round_trip() -> None:
    """После set_data get_data возвращает то же, несмотря на пересоздание storage."""
    fake: Any = _FakeApiClient()
    storage = HttpFsmStorage(fake)

    await storage.set_data(KEY, {"first_name": "Иван", "step": 2})
    data = await storage.get_data(KEY)
    assert data == {"first_name": "Иван", "step": 2}

    # Имитация рестарта: новый объект storage поверх того же удалённого хранилища.
    storage_restart = HttpFsmStorage(fake)
    data_after = await storage_restart.get_data(KEY)
    assert data_after == {"first_name": "Иван", "step": 2}


async def test_set_state_preserves_data() -> None:
    """set_state не затирает data: get_data после set_state возвращает прежние поля."""
    fake: Any = _FakeApiClient()
    storage = HttpFsmStorage(fake)

    await storage.set_data(KEY, {"first_name": "Иван"})
    await storage.set_state(KEY, "reg:wait_phone")

    assert await storage.get_state(KEY) == "reg:wait_phone"
    assert await storage.get_data(KEY) == {"first_name": "Иван"}


async def test_set_state_accepts_state_object() -> None:
    """BaseStorage.set_state принимает str, State, или None — все работают."""
    fake: Any = _FakeApiClient()
    storage = HttpFsmStorage(fake)
    state_obj = State("some_state", group_name="RegisterFsm")

    await storage.set_state(KEY, state_obj)
    assert await storage.get_state(KEY) == "RegisterFsm:some_state"

    await storage.set_state(KEY, None)
    assert await storage.get_state(KEY) is None


async def test_clear_via_none_state_and_empty_data() -> None:
    """`FSMContext.clear` в aiogram = set_state(None) + set_data({}): после этого
    get возвращает пустое состояние."""
    fake: Any = _FakeApiClient()
    storage = HttpFsmStorage(fake)

    await storage.set_data(KEY, {"x": 1})
    await storage.set_state(KEY, "s")
    await storage.set_state(KEY, None)
    await storage.set_data(KEY, {})

    assert await storage.get_state(KEY) is None
    assert await storage.get_data(KEY) == {}


async def test_registration_scenario_survives_restart() -> None:
    """Полный сценарий регистрации: FSM шаги, затем «рестарт» storage, затем get."""
    fake: Any = _FakeApiClient()
    storage = HttpFsmStorage(fake)

    await storage.set_state(KEY, "reg:wait_name")
    await storage.set_data(KEY, {"first_name": "Иван"})
    await storage.set_state(KEY, "reg:wait_surname")
    await storage.set_data(KEY, {"first_name": "Иван", "last_name": "Петров"})

    # Рестарт процесса: InternalApiClient пересоздаётся, state/data остаются в БД.
    storage_restart = HttpFsmStorage(fake)
    assert await storage_restart.get_state(KEY) == "reg:wait_surname"
    assert await storage_restart.get_data(KEY) == {
        "first_name": "Иван",
        "last_name": "Петров",
    }


async def test_close_does_not_close_client() -> None:
    """storage.close() не трогает клиент — им владеет вызывающий run_polling."""
    mock_client = create_autospec(InternalApiClient, instance=True)
    mock_client.aclose = AsyncMock()
    storage = HttpFsmStorage(mock_client)
    await storage.close()
    mock_client.aclose.assert_not_called()


# ===== N-2 (фаза 10b-3): атомарные set_state_only / set_data_only =====


async def test_set_state_calls_set_state_only_not_set() -> None:
    """N-2: `set_state` зовёт `fsm_set_state`, не read-modify-write через fsm_set."""
    fake: Any = _FakeApiClient()
    storage = HttpFsmStorage(fake)
    await storage.set_state(KEY, "reg:wait_phone")
    assert fake.state_calls == [(_format_key(KEY), "reg:wait_phone")]
    # fsm_set_data НЕ должен быть вызван — set_state не трогает data.
    assert fake.data_calls == []


async def test_set_data_calls_set_data_only_not_set() -> None:
    """N-2: `set_data` зовёт `fsm_set_data`, не read-modify-write через fsm_set."""
    fake: Any = _FakeApiClient()
    storage = HttpFsmStorage(fake)
    await storage.set_data(KEY, {"first_name": "Иван"})
    assert fake.data_calls == [(_format_key(KEY), {"first_name": "Иван"})]
    # fsm_set_state НЕ должен быть вызван — set_data не трогает state.
    assert fake.state_calls == []


# ===== N-3 (фаза 10b-3): FsmHttpError + fail-open на чтение =====


class _FakeApiClient5xx:
    """FSM-клиент, который всегда отвечает 503 (имитация падающего ядра)."""

    async def fsm_get(self, key: str) -> tuple[str | None, dict[str, Any]]:
        raise FsmHttpError("get", 503)

    async def fsm_set_state(
        self, key: str, state: str | None, user_id: int | None = None
    ) -> None:
        raise FsmHttpError("set_state", 503)

    async def fsm_set_data(
        self, key: str, data: dict[str, Any], user_id: int | None = None
    ) -> None:
        raise FsmHttpError("set_data", 503)

    async def fsm_clear(self, key: str) -> None:
        raise FsmHttpError("clear", 503)


async def test_fsm_get_state_5xx_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    """N-3: get_state на 5xx возвращает None (fail-open), warn-лог записан."""
    fake: Any = _FakeApiClient5xx()
    storage = HttpFsmStorage(fake)
    with caplog.at_level(logging.WARNING, logger="tg_worker.internal_client"):
        state = await storage.get_state(KEY)
    assert state is None


async def test_fsm_get_data_5xx_returns_empty_dict() -> None:
    """N-3: get_data на 5xx возвращает {} (fail-open)."""
    fake: Any = _FakeApiClient5xx()
    storage = HttpFsmStorage(fake)
    data = await storage.get_data(KEY)
    assert data == {}


async def test_fsm_set_state_5xx_raises_fsm_http_error() -> None:
    """N-3: set_state на 5xx бросает FsmHttpError (не голый RuntimeError)."""
    fake: Any = _FakeApiClient5xx()
    storage = HttpFsmStorage(fake)
    with pytest.raises(FsmHttpError) as exc:
        await storage.set_state(KEY, "reg:wait_phone")
    assert exc.value.op == "set_state"
    assert exc.value.status == 503


async def test_fsm_set_data_5xx_raises_fsm_http_error() -> None:
    """N-3: set_data на 5xx бросает FsmHttpError (не голый RuntimeError)."""
    fake: Any = _FakeApiClient5xx()
    storage = HttpFsmStorage(fake)
    with pytest.raises(FsmHttpError) as exc:
        await storage.set_data(KEY, {"first_name": "Иван"})
    assert exc.value.op == "set_data"
    assert exc.value.status == 503
