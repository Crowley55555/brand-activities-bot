"""Polaroid-фото в Telegram (ADR-40): гость присылает фото, ядро накладывает рамку.

FSM PhotoFrameStates.WaitPhoto держит slug активности. Telegram отдаёт фото как JPEG, самый большой
вариант - последний в списке. Результат приходит сообщением со ссылкой (outbox, шаблон
polaroid_ready).
Бот не ходит в БД (правило 3): решения и лимиты - в ядре.
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
from tg_worker.handlers.states import PhotoFrameStates
from tg_worker.internal_client import InternalApiClient

logger = logging.getLogger("tg_worker.handlers.photo_frame")

PHOTO_FRAME_KIND: Final[str] = "photo_frame"
# Telegram перекодирует фото в JPEG; лимиты MIME и размера проверяет ядро по activities.limits.
TELEGRAM_PHOTO_MIME: Final[str] = "image/jpeg"

router = Router()


def _client() -> InternalApiClient:
    settings = get_worker_settings()
    hmac_key = settings.internal_api_auth_secret
    return InternalApiClient(base_url=settings.internal_api_url, auth_secret=hmac_key)


def photo_error_text(error: str) -> str:
    if error == "already_passed_today":
        return "Сегодня вы уже отправляли фото в эту активность."
    if error == "consent_required":
        return "Чтобы отправить фото, сначала подтвердите согласие: /start."
    if error in ("file_too_large", "mime_not_allowed", "file_size_invalid"):
        return "Фото не подходит. Пришлите другое фото."
    return "Не получилось принять фото. Попробуйте позже."


async def launch_photo_frame(
    cb: CallbackQuery, state: FSMContext, activity: dict[str, Any]
) -> None:
    """Запуск из меню: просим фото и запоминаем slug активности в FSM."""
    if cb.message is None:
        return
    await state.set_state(PhotoFrameStates.WaitPhoto)
    await state.update_data(activity_slug=str(activity.get("slug")))
    await cb.message.answer(
        "Пришлите фото, и мы добавим к нему рамку Polaroid.", reply_markup=menu_only_markup()
    )


@router.message(StateFilter(PhotoFrameStates.WaitPhoto), F.photo)
async def handle_photo(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.photo:
        return
    largest = message.photo[-1]
    data = await state.get_data()
    status, body = await _client().photo_frame_accept(
        channel_type="telegram",
        external_id=str(message.from_user.id),
        activity_slug=str(data.get("activity_slug", "")),
        tg_file_id=largest.file_id,
        mime_type=TELEGRAM_PHOTO_MIME,
        file_size_bytes=largest.file_size or 0,
    )
    await state.clear()
    if status == 200:
        await message.answer(
            "Фото принято, рисуем рамку. Результат придёт отдельным сообщением.",
            reply_markup=menu_only_markup(),
        )
        return
    logger.info("photo_frame_rejected", extra={"event": "photo_frame_rejected", "status": status})
    await message.answer(photo_error_text(str(body.get("error", ""))))
