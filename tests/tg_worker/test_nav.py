# ruff: noqa: F811
"""Навигация tg-бота: `/menu`, `nav:menu`, «← Назад» в UGC и кнопка «⌂ В меню» на экранах.

Апдейты идут через корневой роутер (настоящие фильтры состояний, MemoryStorage). Фикстуры и
фейки ядра берём из test_ugc_handler: там же описана подмена сессии бота.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.methods import SendMessage

from tests.tg_worker.test_ugc_handler import (  # noqa: F401  (фикстуры pytest)
    POST_URL,
    SUBSCRIPTION_URL,
    _Core,
    _feed_callback,
    _feed_message,
    _launch,
    _raw_message,
    _send_photo,
    _send_text,
    _Session,
    _texts,
    bot,
    core,
    session,
    state,
)
from tg_worker.handlers import main_menu, nav
from tg_worker.handlers.states import ConsentStates, RegistrationStates, UgcStates


@pytest.fixture
def menu_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def fake_menu(message: Any) -> None:
        calls.append("menu")

    monkeypatch.setattr(main_menu, "show_main_menu", fake_menu)
    return calls


def _markups(session: _Session) -> list[Any]:
    return [m.reply_markup for m in session.sent if isinstance(m, SendMessage)]


def _callbacks(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_menu_command_clears_any_state_and_shows_menu(
    bot: Bot, state: FSMContext, core: _Core, menu_calls: list[str]
) -> None:
    await _launch(bot, state, post=True, subscription=False)
    assert await state.get_state() == UgcStates.WaitPhoto.state

    await _send_text(bot, state, "/menu")

    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert menu_calls == ["menu"]


async def test_nav_menu_callback_clears_state_and_shows_menu(
    bot: Bot, state: FSMContext, core: _Core, menu_calls: list[str]
) -> None:
    await _launch(bot, state, post=True, subscription=False)

    await _feed_callback(bot, state, nav.NAV_MENU)

    assert await state.get_state() is None
    assert menu_calls == ["menu"]


@pytest.mark.parametrize("fsm_state", [ConsentStates.WaitConsent, RegistrationStates.WaitPhone])
async def test_menu_command_does_not_leave_registration_flow(
    bot: Bot,
    state: FSMContext,
    session: _Session,
    core: _Core,
    menu_calls: list[str],
    fsm_state: Any,
) -> None:
    await state.set_state(fsm_state)

    await _send_text(bot, state, "/menu")

    assert await state.get_state() == fsm_state.state
    assert menu_calls == []
    assert _texts(session) == [nav.FINISH_REGISTRATION_TEXT]


async def test_ugc_back_from_subscription_returns_to_post_step(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    await _launch(bot, state, post=True, subscription=True)
    await _send_photo(bot, state)
    await _send_text(bot, state, POST_URL)
    assert await state.get_state() == UgcStates.WaitSubscriptionUrl.state

    await _feed_callback(bot, state, nav.NAV_UGC_BACK)

    assert await state.get_state() == UgcStates.WaitPostUrl.state
    data = await state.get_data()
    assert data["subscription_url"] is None
    assert core.accept_calls == []
    assert "ссылку на пост" in _texts(session)[-1]


async def test_ugc_back_from_post_step_returns_to_photo_and_drops_photo(
    bot: Bot, state: FSMContext, core: _Core
) -> None:
    await _launch(bot, state, post=True, subscription=False)
    await _send_photo(bot, state)
    assert await state.get_state() == UgcStates.WaitPostUrl.state

    await _feed_callback(bot, state, nav.NAV_UGC_BACK)

    assert await state.get_state() == UgcStates.WaitPhoto.state
    data = await state.get_data()
    assert data["tg_file_id"] is None and data["post_url"] is None
    assert data["attempt_id"] is not None  # прохождение в ядре остаётся открытым


async def test_ugc_screens_carry_menu_button_and_back_only_on_link_steps(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    await _launch(bot, state, post=True, subscription=True)
    await _send_photo(bot, state)
    await _send_text(bot, state, POST_URL)
    await _send_text(bot, state, SUBSCRIPTION_URL)

    markups = _markups(session)
    assert _callbacks(markups[0]) == [nav.NAV_MENU]  # шаг «фото»
    assert _callbacks(markups[1]) == [nav.NAV_UGC_BACK, nav.NAV_MENU]  # ссылка на пост
    assert _callbacks(markups[2]) == [nav.NAV_UGC_BACK, nav.NAV_MENU]  # ссылка на подписку
    assert _callbacks(markups[3]) == [nav.NAV_MENU]  # фото принято


async def test_menu_text_is_not_swallowed_by_ugc_hint(
    bot: Bot, state: FSMContext, session: _Session, core: _Core, menu_calls: list[str]
) -> None:
    """/menu приходит раньше `handle_unexpected` UGC: подсказки «нужно фото» не будет."""
    await _launch(bot, state, post=False, subscription=False)
    before = len(_texts(session))

    await _feed_message(bot, state, _raw_message(text="/menu"))

    assert len(_texts(session)) == before
