"""Команда /forget и двойное подтверждение (фаза 10b-3, I-2).

Канон 02 §9 + правило 9: `/forget` — право гостя на отзыв; удаление с
двойным подтверждением (ровно слово «ПОДТВЕРЖДАЮ»).

Правило 3 (headless-ядро): весь каскад (анонимизация, UGC/FSM, s3_delete) — в
`app/core/personal_data/service.py`. Здесь только парсинг команды, FSM
подтверждения и рендер ответа.

Правило 2 (ПДн в логах): логируем только `event`, `status`, ошибки — ни телефон,
ни email, ни имя гостя в сыром виде. `external_id` (chat_id) не ПДн по канону 02,
но тоже не кладём, чтобы не разводить привычку.
"""

from __future__ import annotations

import logging
from typing import Final

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from tg_worker.config import get_worker_settings
from tg_worker.handlers.nav import menu_only_markup
from tg_worker.handlers.states import ForgetStates
from tg_worker.internal_client import InternalApiClient

logger = logging.getLogger("tg_worker.handlers.forget")

router = Router()

# Канон 02 §9: ровно одно слово, в верхнем регистре, без пробелов по краям.
CONFIRM_WORD: Final[str] = "ПОДТВЕРЖДАЮ"

_WARNING_TEXT: Final[str] = (
    "Это удалит все ваши персональные данные и загруженные материалы.\n"
    "Если точно хотите продолжить, отправьте одним сообщением слово "
    f"«{CONFIRM_WORD}». Для отмены — /cancel или любой другой текст."
)
_CANCELLED_TEXT: Final[str] = "Отменено. Ваши данные не изменились."
_DONE_TEXT: Final[str] = (
    "Готово: ваши данные удалены. Чтобы снова пользоваться ботом, запустите /start."
)
_NOT_REGISTERED_TEXT: Final[str] = (
    "У вас нет сохранённых данных для удаления. "
    "Если нужна помощь — /start."
)
_ALREADY_TEXT: Final[str] = "Ваши данные уже удалены ранее."
_UNEXPECTED_TEXT: Final[str] = (
    "Не получилось обработать запрос, попробуйте позже."
)


def _client() -> InternalApiClient:
    settings = get_worker_settings()
    hmac_key = settings.internal_api_auth_secret
    return InternalApiClient(
        base_url=settings.internal_api_url, auth_secret=hmac_key
    )


@router.message(Command("forget"))
async def handle_forget(message: Message, state: FSMContext) -> None:
    """Вход в FSM: показать предупреждение, ждать «ПОДТВЕРЖДАЮ»."""
    await state.clear()
    if message.from_user is None:
        return
    await state.set_state(ForgetStates.WaitConfirm)
    await state.update_data(external_id=str(message.from_user.id))
    await message.answer(_WARNING_TEXT)


@router.message(Command("cancel"), ForgetStates.WaitConfirm)
async def handle_cancel_in_forget(message: Message, state: FSMContext) -> None:
    """Отмена через /cancel: FSM чистим, показываем «Отменено»."""
    await state.clear()
    await message.answer(_CANCELLED_TEXT, reply_markup=menu_only_markup())


@router.message(ForgetStates.WaitConfirm)
async def handle_forget_confirm(message: Message, state: FSMContext) -> None:
    """Строго «ПОДТВЕРЖДАЮ» — вызов ядра; любой другой текст — отмена."""
    text = (message.text or "").strip()
    if text != CONFIRM_WORD:
        await state.clear()
        logger.info("forget_cancelled", extra={"event": "forget_cancelled"})
        await message.answer(_CANCELLED_TEXT, reply_markup=menu_only_markup())
        return

    data = await state.get_data()
    external_id = str(data.get("external_id") or "")
    await state.clear()
    status, body = await _client().forget_user(
        channel_type="telegram", external_id=external_id
    )
    if status == 200:
        logger.info(
            "forget_done",
            extra={
                "event": "forget_done",
                "user_id": int(body.get("user_id") or 0),
                "ugc_deleted": int(body.get("ugc_submissions_deleted") or 0),
                "fsm_deleted": int(body.get("tg_fsm_rows_deleted") or 0),
                "s3_delete_enqueued": int(body.get("s3_delete_jobs_enqueued") or 0),
            },
        )
        await message.answer(_DONE_TEXT)
        return

    error = str(body.get("error", "")) if isinstance(body, dict) else ""
    if status == 404 and error == "channel_not_found":
        await message.answer(_NOT_REGISTERED_TEXT)
        return
    if status == 409 and error == "not_registered":
        await message.answer(_NOT_REGISTERED_TEXT)
        return
    if status == 409 and error == "already_anonymized":
        await message.answer(_ALREADY_TEXT)
        return
    logger.warning(
        "forget_failed",
        extra={"event": "forget_failed", "status": status, "error": error},
    )
    await message.answer(_UNEXPECTED_TEXT)
