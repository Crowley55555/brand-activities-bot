"""B1 (ревью фазы 4): меню бренда показывает все активности, опрос не теряется рядом с бронью.

Вызываем обработчики напрямую с подменными callback/state и клиентом ядра: проверяем, что
уходит в launch_survey и show_booking_slots и какие кнопки показаны.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import InlineKeyboardMarkup

from tg_worker.handlers import main_menu

BRAND_A_SURVEY: dict[str, Any] = {
    "slug": "brand_a_skin_survey",
    "display_name": "Код кожи",
    "kind": "survey",
}
BRAND_A_BOOKING: dict[str, Any] = {
    "slug": "brand_a_diagnostics_booking",
    "display_name": "Диагностика кожи",
    "kind": "booking",
}


class _Message:
    def __init__(self) -> None:
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None) -> None:
        self.answers.append((text, reply_markup))


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _Message()
        self.from_user = SimpleNamespace(id=4242)

    async def answer(self) -> None:
        return None


class _State:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def update_data(self, data: Any = None, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)


class _Client:
    def __init__(self, activities: list[dict[str, Any]]) -> None:
        self._activities = activities

    async def list_brand_activities(self, slug: str) -> list[dict[str, Any]]:
        return self._activities


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    recorded: dict[str, list[Any]] = {"survey": [], "booking": []}

    async def fake_survey(cb: Any, state: Any, activity: dict[str, Any]) -> None:
        recorded["survey"].append(activity)

    async def fake_booking(cb: Any, state: Any, slug: str) -> None:
        recorded["booking"].append(slug)

    monkeypatch.setattr(main_menu, "launch_survey", fake_survey)
    monkeypatch.setattr(main_menu, "show_booking_slots", fake_booking)
    return recorded


def _brand_has(monkeypatch: pytest.MonkeyPatch, activities: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(main_menu, "_client", lambda: _Client(activities))


async def test_brand_with_single_activity_goes_direct(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]
) -> None:
    """Регресс: бренд с одной активностью (опрос) открывает её сразу, как раньше."""
    _brand_has(monkeypatch, [BRAND_A_SURVEY])
    cb = _Callback("brand:brand_a")

    await main_menu.handle_brand_click(cb, _State())

    assert calls["survey"] == [BRAND_A_SURVEY]
    assert calls["booking"] == []
    assert cb.message.answers == []


async def test_brand_with_two_activities_shows_keyboard(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]
) -> None:
    """B1: Бренд A с опросом и записью - две кнопки, ни одна активность не запускается сама."""
    _brand_has(monkeypatch, [BRAND_A_SURVEY, BRAND_A_BOOKING])
    cb = _Callback("brand:brand_a")
    state = _State()

    await main_menu.handle_brand_click(cb, state)

    assert calls == {"survey": [], "booking": []}
    assert len(cb.message.answers) == 1
    _, markup = cb.message.answers[0]
    assert isinstance(markup, InlineKeyboardMarkup)
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert [b.callback_data for b in buttons] == [
        "activity:brand_a_skin_survey",
        "activity:brand_a_diagnostics_booking",
    ]
    assert (await state.get_data())["brand_activities"] == [BRAND_A_SURVEY, BRAND_A_BOOKING]


async def test_activity_callback_survey_launches_survey(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]
) -> None:
    _brand_has(monkeypatch, [BRAND_A_SURVEY, BRAND_A_BOOKING])
    state = _State()
    await main_menu.handle_brand_click(_Callback("brand:brand_a"), state)

    await main_menu.handle_activity_click(_Callback("activity:brand_a_skin_survey"), state)

    assert calls["survey"] == [BRAND_A_SURVEY]
    assert calls["booking"] == []


async def test_activity_callback_booking_shows_slots(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]
) -> None:
    _brand_has(monkeypatch, [BRAND_A_SURVEY, BRAND_A_BOOKING])
    state = _State()
    await main_menu.handle_brand_click(_Callback("brand:brand_a"), state)

    await main_menu.handle_activity_click(
        _Callback("activity:brand_a_diagnostics_booking"), state
    )

    assert calls["booking"] == ["brand_a_diagnostics_booking"]
    assert calls["survey"] == []


async def test_activity_callback_without_brand_state_asks_to_retry(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]
) -> None:
    """Нет списка активностей в FSM: ничего не запускаем, просим выбрать бренд заново."""
    cb = _Callback("activity:brand_a_skin_survey")

    await main_menu.handle_activity_click(cb, _State())

    assert calls == {"survey": [], "booking": []}
    assert cb.message.answers[0][0] == "Выберите бренд заново."
