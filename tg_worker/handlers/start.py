from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from tg_worker.config import get_worker_settings
from tg_worker.handlers.main_menu import show_main_menu
from tg_worker.handlers.states import ConsentStates
from tg_worker.internal_client import InternalApiClient

logger = logging.getLogger("tg_worker.handlers.start")

router = Router()


def _client() -> InternalApiClient:
    settings = get_worker_settings()
    hmac_key = settings.internal_api_auth_secret
    return InternalApiClient(
        base_url=settings.internal_api_url, auth_secret=hmac_key
    )


@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext) -> None:
    """На /start: отправляем Update в ядро, затем предлагаем принять политику."""
    client = _client()
    raw_update = {
        "update_id": 0,
        "message": {
            "message_id": message.message_id,
            "date": int(message.date.timestamp()) if message.date else 0,
            "chat": {
                "id": message.chat.id,
                "type": message.chat.type,
            },
            "from": {
                "id": message.from_user.id if message.from_user else 0,
                "is_bot": bool(message.from_user and message.from_user.is_bot),
                "username": message.from_user.username
                if message.from_user
                else None,
            },
            "text": message.text or "",
        },
    }
    try:
        response = await client.send_update(raw_update, datetime.now(tz=UTC))
    except Exception:
        logger.exception("internal_api_failed")
        return

    # Если гость уже зарегистрирован (consent принят + ПДн записаны),
    # пропускаем политику и FSM, сразу показываем меню брендов.
    if response.get("is_registered"):
        await state.clear()
        await message.answer("С возвращением! Выберите бренд:")
        await show_main_menu(message)
        return

    # ADR-7: FSM-состояние хранится в tg_worker. Переводим в WaitConsent.
    await state.clear()
    await state.set_state(ConsentStates.WaitConsent)
    external_id = str(message.from_user.id) if message.from_user else "0"
    await state.update_data(external_id=external_id)

    text = (
        "Привет! Прежде чем продолжить, прочтите политику обработки данных "
        "и нажмите «Принимаю политику»."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Принимаю политику", callback_data="consent:accept"
                )
            ]
        ]
    )
    await message.answer(text, reply_markup=keyboard)
