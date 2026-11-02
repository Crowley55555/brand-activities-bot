"""Подарок за пост в Telegram (ADR-42): гость присылает ссылку на публикацию, модератор проверяет.

FSM ConfirmationStates.WaitPostLink держит slug активности. Ссылку проверяет ядро (http/https,
длина).
Неверная ссылка не сбрасывает ожидание: гость может прислать ссылку ещё раз. Решение модератора
приходит отдельным сообщением из outbox (promocode или confirmation_rejected).
"""

from __future__ import annotations

import logging
from typing import Any, Final

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from tg_worker.config import get_worker_settings
from tg_worker.handlers.nav import menu_only_markup
from tg_worker.handlers.states import ConfirmationStates
from tg_worker.internal_client import InternalApiClient

logger = logging.getLogger("tg_worker.handlers.confirmation")

CONFIRMATION_KIND: Final[str] = "confirmation"

router = Router()


def _client() -> InternalApiClient:
    settings = get_worker_settings()
    hmac_key = settings.internal_api_auth_secret
    return InternalApiClient(base_url=settings.internal_api_url, auth_secret=hmac_key)


def confirmation_error_text(error: str) -> str:
    if error == "already_passed_today":
        return "Сегодня вы уже отправляли ссылку в эту активность."
    if error == "consent_required":
        return "Чтобы отправить ссылку, сначала подтвердите согласие: /start."
    return "Не получилось принять ссылку. Попробуйте позже."


async def launch_confirmation(
    cb: CallbackQuery, state: FSMContext, activity: dict[str, Any]
) -> None:
    """Запуск из меню: просим ссылку на пост и запоминаем slug активности в FSM."""
    if cb.message is None:
        return
    await state.set_state(ConfirmationStates.WaitPostLink)
    await state.update_data(activity_slug=str(activity.get("slug")))
    await cb.message.answer(
        "Пришлите ссылку на ваш пост с публикацией.", reply_markup=menu_only_markup()
    )


@router.message(StateFilter(ConfirmationStates.WaitPostLink), F.text)
async def handle_post_link(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.text is None:
        return
    post_url = message.text.strip()
    data = await state.get_data()
    status, body = await _client().confirmation_accept(
        channel_type="telegram",
        external_id=str(message.from_user.id),
        activity_slug=str(data.get("activity_slug", "")),
        post_url=post_url,
    )
    if status == 200:
        await state.clear()
        await message.answer(
            "Ссылка принята. Модератор проверит публикацию и пришлёт ответ.",
            reply_markup=menu_only_markup(),
        )
        return
    error = str(body.get("error", ""))
    if error == "post_url_invalid":
        await message.answer("Пришлите ссылку, которая начинается с https://")
        return
    await state.clear()
    logger.info(
        "confirmation_rejected_at_accept",
        extra={"event": "confirmation_rejected_at_accept", "status": status},
    )
    await message.answer(confirmation_error_text(error))
