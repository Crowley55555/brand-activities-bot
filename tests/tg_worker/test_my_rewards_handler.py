"""tg_worker /my (фаза 10b-1, C-1): активные ваучеры/подарки + «подойду сейчас».

Вызываем обработчики напрямую с подменным InternalApiClient. Правило 2: handler
логирует только `reward_id`/статус, OTP в логах не светится. Канон 04 §1.2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import InlineKeyboardMarkup

from tg_worker.handlers import rewards as rewards_handler

NOW = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)


class _Message:
    def __init__(self) -> None:
        self.answers: list[tuple[str, Any, dict[str, Any]]] = []
        self.from_user = SimpleNamespace(id=5151)

    async def answer(
        self,
        text: str,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> None:
        self.answers.append((text, reply_markup, kwargs))


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _Message()
        self.from_user = SimpleNamespace(id=5151)

    async def answer(self) -> None:
        return None


class _State:
    def __init__(self) -> None:
        self.cleared = False

    async def clear(self) -> None:
        self.cleared = True


class _Client:
    def __init__(self) -> None:
        self.rewards_response: list[dict[str, Any]] = []
        self.rewards_exception: Exception | None = None
        self.otp_responses: list[tuple[int, dict[str, Any]]] = []
        self.list_calls: list[tuple[str, str]] = []
        self.otp_calls: list[tuple[str, str, int]] = []

    async def list_active_rewards(
        self, channel_type: str, external_id: str
    ) -> list[dict[str, Any]]:
        self.list_calls.append((channel_type, external_id))
        if self.rewards_exception is not None:
            raise self.rewards_exception
        return self.rewards_response

    async def request_reward_otp(
        self, channel_type: str, external_id: str, reward_id: int
    ) -> tuple[int, dict[str, Any]]:
        self.otp_calls.append((channel_type, external_id, reward_id))
        return self.otp_responses.pop(0)


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> _Client:
    fake = _Client()
    monkeypatch.setattr(rewards_handler, "_client", lambda: fake)
    return fake


# ----- 22. /my показывает две активные + inline-кнопки «подойду сейчас» ------


async def test_my_shows_active_vouchers(core: _Client) -> None:
    """Моком отдаём 2 активные (voucher+physical_gift) + 1 "redeemed" (её нет в списке ядром).

    Ядро возвращает только активные (list_active_named_rewards_by_user), поэтому
    тест даёт моку только эти две — проверяем, что обе кнопки появились.
    """
    core.rewards_response = [
        {
            "reward_id": 101,
            "reward_type": "voucher",
            "title": "Напиток дня",
            "issued_at": NOW.isoformat(),
        },
        {
            "reward_id": 102,
            "reward_type": "physical_gift",
            "title": "Пробник крема",
            "issued_at": NOW.isoformat(),
        },
    ]
    message = _Message()
    state = _State()
    await rewards_handler.handle_my(message, state)  # type: ignore[arg-type]

    assert core.list_calls == [("telegram", "5151")]
    assert state.cleared
    assert len(message.answers) >= 1
    first_text, first_markup, _ = message.answers[0]
    assert "Напиток дня" in first_text
    assert "Пробник крема" in first_text
    assert isinstance(first_markup, InlineKeyboardMarkup)
    buttons = [b for row in first_markup.inline_keyboard for b in row]
    cbdatas = {b.callback_data for b in buttons}
    assert "otp:101" in cbdatas
    assert "otp:102" in cbdatas


async def test_my_empty_list_shows_hint(core: _Client) -> None:
    """Нет активных наград — текст-подсказка без кнопок OTP."""
    core.rewards_response = []
    message = _Message()
    state = _State()
    await rewards_handler.handle_my(message, state)  # type: ignore[arg-type]
    assert message.answers
    text, markup, _ = message.answers[0]
    assert "Пока у вас нет" in text
    # Нет OTP-кнопок
    if isinstance(markup, InlineKeyboardMarkup):
        for row in markup.inline_keyboard:
            for button in row:
                cb = button.callback_data or ""
                assert not cb.startswith("otp:")


# ----- 23. callback otp:<id> показывает OTP крупно + TTL ---------------------


async def test_my_otp_callback_shows_code_in_chat(core: _Client) -> None:
    core.otp_responses = [(200, {"otp": "456789", "ttl_minutes": 5})]
    cb = _Callback("otp:101")
    await rewards_handler.handle_otp_callback(cb, _State())  # type: ignore[arg-type]

    assert core.otp_calls == [("telegram", "5151", 101)]
    assert cb.message.answers, "handler должен ответить сообщением"
    text, _markup, kwargs = cb.message.answers[0]
    assert "456789" in text
    assert "5 мин" in text or "5 мин." in text
    # OTP крупно — внутри <b><code>...</code></b> и parse_mode=HTML
    assert "<b>" in text and "<code>" in text
    assert kwargs.get("parse_mode") == "HTML"


async def test_my_otp_callback_rejects(core: _Client) -> None:
    """Ошибка ядра -> понятный текст, без внутренних кодов."""
    core.otp_responses = [(409, {"error": "already_redeemed"})]
    cb = _Callback("otp:999")
    await rewards_handler.handle_otp_callback(cb, _State())  # type: ignore[arg-type]
    assert cb.message.answers
    text, *_ = cb.message.answers[0]
    assert "уже погашена" in text


async def test_my_otp_callback_bad_data(core: _Client) -> None:
    """Мусор в callback_data — текст-ошибка, без запроса в ядро."""
    cb = _Callback("otp:abc")
    await rewards_handler.handle_otp_callback(cb, _State())  # type: ignore[arg-type]
    assert core.otp_calls == []
    assert cb.message.answers


# ----- Доп: отображение названия без title → fallback по типу ----------------


def test_title_fallbacks() -> None:
    assert rewards_handler._title_for({"reward_type": "voucher"}) == "Ваучер"
    assert (
        rewards_handler._title_for({"reward_type": "physical_gift"})
        == "Подарок у стойки"
    )
    assert rewards_handler._title_for({"reward_type": "voucher", "title": " "}) == "Ваучер"


def test_otp_error_text_known_and_unknown() -> None:
    assert "уже погашена" in rewards_handler.otp_error_text("already_redeemed")
    assert "/start" in rewards_handler.otp_error_text("consent_required")
    assert rewards_handler.otp_error_text("какая-то дичь").startswith("Не получилось")
