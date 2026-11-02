"""tg_worker /forget (фаза 10b-3, I-2): двойное подтверждение «ПОДТВЕРЖДАЮ».

Вызываем обработчики напрямую с подменным InternalApiClient. Правило 2: handler
логирует только `event` и счётчики из ответа ядра, ПДн нет. Правило 9: удаление
только с явным «ПОДТВЕРЖДАЮ» — любой другой текст сбрасывает FSM.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tg_worker.handlers import forget as forget_handler
from tg_worker.handlers.states import ForgetStates


class _Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[tuple[str, Any]] = []
        self.from_user = SimpleNamespace(id=5151)

    async def answer(self, text: str, reply_markup: Any = None, **_: Any) -> None:
        self.answers.append((text, reply_markup))


class _State:
    """Фейковый FSMContext: ловит set_state, update_data, clear."""

    def __init__(self) -> None:
        self.state: Any = None
        self.data: dict[str, Any] = {}
        self.cleared: int = 0

    async def set_state(self, state: Any) -> None:
        self.state = state

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def clear(self) -> None:
        self.cleared += 1
        self.state = None


class _Client:
    def __init__(self) -> None:
        self.forget_responses: list[tuple[int, dict[str, Any]]] = []
        self.calls: list[tuple[str, str]] = []

    async def forget_user(
        self, channel_type: str, external_id: str
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((channel_type, external_id))
        return self.forget_responses.pop(0)


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> _Client:
    fake = _Client()
    monkeypatch.setattr(forget_handler, "_client", lambda: fake)
    return fake


async def test_forget_shows_warning_and_waits_confirm(core: _Client) -> None:
    """/forget → предупреждение с «ПОДТВЕРЖДАЮ», FSM = WaitConfirm."""
    msg = _Message("/forget")
    state = _State()
    await forget_handler.handle_forget(msg, state)
    assert state.state == ForgetStates.WaitConfirm
    assert state.data == {"external_id": "5151"}
    assert len(msg.answers) == 1
    text = msg.answers[0][0]
    assert "ПОДТВЕРЖДАЮ" in text
    # Ядро не дёрнули — только предупреждение.
    assert core.calls == []


async def test_forget_wrong_confirm_cancels(core: _Client) -> None:
    """Любой другой текст → «Отменено», FSM clear, ядро не вызывается."""
    msg = _Message("да")
    state = _State()
    state.data = {"external_id": "5151"}
    await forget_handler.handle_forget_confirm(msg, state)
    assert state.cleared == 1
    assert core.calls == []
    assert "Отменено" in msg.answers[0][0]


async def test_forget_cancel_command_resets_fsm(core: _Client) -> None:
    """/cancel в WaitConfirm → «Отменено», FSM clear."""
    msg = _Message("/cancel")
    state = _State()
    state.state = ForgetStates.WaitConfirm
    await forget_handler.handle_cancel_in_forget(msg, state)
    assert state.cleared == 1
    assert "Отменено" in msg.answers[0][0]
    assert core.calls == []


async def test_forget_right_confirm_calls_core(core: _Client) -> None:
    """Текст «ПОДТВЕРЖДАЮ» → вызов forget_user + «Готово»."""
    msg = _Message("ПОДТВЕРЖДАЮ")
    state = _State()
    state.data = {"external_id": "5151"}
    core.forget_responses.append(
        (
            200,
            {
                "status": "ok",
                "user_id": 42,
                "ugc_submissions_deleted": 2,
                "tg_fsm_rows_deleted": 1,
                "s3_delete_jobs_enqueued": 2,
            },
        )
    )
    await forget_handler.handle_forget_confirm(msg, state)
    assert core.calls == [("telegram", "5151")]
    assert state.cleared == 1
    assert "Готово" in msg.answers[0][0]


async def test_forget_not_registered_shows_friendly_message(core: _Client) -> None:
    """409 not_registered → гость видит «У вас нет сохранённых данных для удаления»."""
    msg = _Message("ПОДТВЕРЖДАЮ")
    state = _State()
    state.data = {"external_id": "5151"}
    core.forget_responses.append((409, {"error": "not_registered"}))
    await forget_handler.handle_forget_confirm(msg, state)
    assert "нет сохранённых данных" in msg.answers[0][0]


async def test_forget_already_anonymized_shows_message(core: _Client) -> None:
    """409 already_anonymized → гость видит «данные уже удалены ранее»."""
    msg = _Message("ПОДТВЕРЖДАЮ")
    state = _State()
    state.data = {"external_id": "5151"}
    core.forget_responses.append((409, {"error": "already_anonymized"}))
    await forget_handler.handle_forget_confirm(msg, state)
    assert "уже удалены" in msg.answers[0][0]


async def test_forget_5xx_shows_generic_text(core: _Client) -> None:
    """5xx или неизвестная ошибка → общий текст, FSM clear."""
    msg = _Message("ПОДТВЕРЖДАЮ")
    state = _State()
    state.data = {"external_id": "5151"}
    core.forget_responses.append((500, {}))
    await forget_handler.handle_forget_confirm(msg, state)
    assert "попробуйте позже" in msg.answers[0][0]
    assert state.cleared == 1
