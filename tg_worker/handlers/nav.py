"""Навигация в tg-боте: «⌂ В меню», «← Назад» и команда /menu (чисто адаптерская логика).

Хелперы клавиатур не импортируют main_menu (он импортирует хендлеры активностей, а те - этот
модуль), поэтому `show_main_menu` подтягивается лениво внутри обработчиков.
В лог пишем только event и activity_slug (правило 2): ПДн и ссылки сюда не попадают.
"""

from __future__ import annotations

import logging
from typing import Final

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from tg_worker.handlers.states import ConsentStates, RegistrationStates

logger = logging.getLogger("tg_worker.handlers.nav")

router = Router()

NAV_MENU: Final[str] = "nav:menu"
NAV_UGC_BACK: Final[str] = "nav:ugc_back"
MENU_TEXT: Final[str] = "⌂ В меню"
BACK_TEXT: Final[str] = "← Назад"
BRANDS_TEXT: Final[str] = "← Бренды"
FINISH_REGISTRATION_TEXT: Final[str] = "Сначала завершите регистрацию. Если запутались: /start."


def menu_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text=MENU_TEXT, callback_data=NAV_MENU)


def back_button(cb_data: str, text: str = BACK_TEXT) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb_data)


def with_menu(
    rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    """Клавиатура: переданные ряды и последней строкой «⌂ В меню»."""
    return InlineKeyboardMarkup(inline_keyboard=[*(rows or []), [menu_button()]])


def menu_only_markup() -> InlineKeyboardMarkup:
    return with_menu()


async def _to_menu(message: Message, state: FSMContext) -> None:
    from tg_worker.handlers.main_menu import show_main_menu

    slug = (await state.get_data()).get("activity_slug")
    await state.clear()
    logger.info("nav_menu", extra={"event": "nav_menu", "activity_slug": slug})
    await show_main_menu(message)


@router.message(
    Command("menu"), StateFilter(ConsentStates, RegistrationStates)
)
async def handle_menu_command_unregistered(message: Message) -> None:
    """Согласие и регистрация - критический flow: из него /menu не выпускает."""
    await message.answer(FINISH_REGISTRATION_TEXT)


@router.message(Command("menu"))
async def handle_menu_command(message: Message, state: FSMContext) -> None:
    await _to_menu(message, state)


@router.callback_query(F.data == NAV_MENU)
async def handle_nav_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if cb.message is None or not hasattr(cb.message, "answer"):
        return
    await _to_menu(cb.message, state)  # type: ignore[arg-type]
