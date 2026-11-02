"""Команда /my и callback «подойду сейчас» (фаза 10b-1, C-1).

/my — выводит гостю его активные именные награды (voucher, physical_gift) с кнопкой
«подойду сейчас». Клик по кнопке просит у ядра OTP и отрисовывает его одним крупным
сообщением с указанием TTL из ответа ядра (5 минут по умолчанию).

Правило 3 (headless-ядро): генерация OTP, фильтрация именных, срок жизни — всё в
`app/core/rewards/otp.py`. Здесь только разбор команды/callback'а и рендер.
Правило 2 (ПДн в логах): логируем только `event`, `reward_id`, статус; OTP, имя
гостя, telegram_username, phone/email — никогда.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from tg_worker.config import get_worker_settings
from tg_worker.handlers.nav import menu_only_markup
from tg_worker.handlers.states import ConsentStates, RegistrationStates
from tg_worker.internal_client import InternalApiClient

logger = logging.getLogger("tg_worker.handlers.rewards")

router = Router()

# Callback-данные: «otp:<reward_id>». Канон 02 §7 — reward_id помещается в 64 байта.
OTP_CALLBACK_PREFIX: Final[str] = "otp:"
# Текст кнопки под каждой активной именной наградой.
_OTP_BUTTON_TEXT: Final[str] = "подойду сейчас"
# Пустой список в ядре — у гостя нет ваучеров/подарков.
_EMPTY_TEXT: Final[str] = (
    "Пока у вас нет активных ваучеров или подарков. Пройдите активности брендов из меню."
)
_WAIT_REGISTRATION_TEXT: Final[str] = (
    "Сначала завершите регистрацию. Если запутались: /start."
)


def _client() -> InternalApiClient:
    settings = get_worker_settings()
    hmac_key = settings.internal_api_auth_secret
    return InternalApiClient(
        base_url=settings.internal_api_url, auth_secret=hmac_key
    )


def _title_for(reward: dict[str, Any]) -> str:
    """Короткая подпись для строки меню: title или fallback по типу."""
    title = reward.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    reward_type = reward.get("reward_type")
    if reward_type == "voucher":
        return "Ваучер"
    if reward_type == "physical_gift":
        return "Подарок у стойки"
    return "Награда"


def _row(reward: dict[str, Any]) -> list[InlineKeyboardButton]:
    rid = int(reward.get("reward_id") or 0)
    return [
        InlineKeyboardButton(
            text=f"{_title_for(reward)} — {_OTP_BUTTON_TEXT}",
            callback_data=f"{OTP_CALLBACK_PREFIX}{rid}",
        )
    ]


def otp_error_text(error: str) -> str:
    """Ошибка ядра -> текст гостю; внутренние коды наружу не выпускаем."""
    if error == "already_redeemed":
        return "Эта награда уже погашена. Проверьте «мои награды» командой /my."
    if error == "reward_revoked":
        return "Эта награда отозвана."
    if error == "reward_not_eligible":
        return "Эта награда не требует OTP — покажите её администратору как есть."
    if error == "reward_not_found":
        return "Такой награды не нашлось. Откройте /my и попробуйте ещё раз."
    if error == "consent_required":
        return "Сначала подтвердите согласие: /start."
    if error == "channel_not_found":
        return "Чтобы получить код, сначала зарегистрируйтесь: /start."
    return "Не получилось получить код. Попробуйте позже."


@router.message(
    Command("my"), StateFilter(ConsentStates, RegistrationStates)
)
async def handle_my_unregistered(message: Message) -> None:
    """/my во время регистрации не прерывает критический flow."""
    await message.answer(_WAIT_REGISTRATION_TEXT)


@router.message(Command("my"))
async def handle_my(message: Message, state: FSMContext) -> None:
    """Показать активные именные награды + кнопка «подойду сейчас» на каждой."""
    await state.clear()
    if message.from_user is None:
        return
    try:
        rewards = await _client().list_active_rewards(
            "telegram", str(message.from_user.id)
        )
    except Exception:
        # Не раскрываем внутренности сети/HMAC — показываем общий текст.
        logger.exception("my_rewards_fetch_failed")
        await message.answer(
            "Не получилось загрузить ваши награды. Попробуйте позже.",
            reply_markup=menu_only_markup(),
        )
        return

    if not rewards:
        await message.answer(_EMPTY_TEXT, reply_markup=menu_only_markup())
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[_row(reward) for reward in rewards] + [[]]
    )
    lines = [
        f"• {_title_for(reward)}" for reward in rewards
    ]
    text = "Ваши активные ваучеры и подарки:\n" + "\n".join(lines)
    await message.answer(text, reply_markup=keyboard)
    # Отдельное сообщение-клавиатура «⌂ В меню», чтобы не класть её среди OTP-кнопок.
    await message.answer(" ", reply_markup=menu_only_markup())


@router.callback_query(F.data.startswith(OTP_CALLBACK_PREFIX))
async def handle_otp_callback(cb: CallbackQuery, state: FSMContext) -> None:
    """Запросить OTP у ядра и показать гостю одним крупным сообщением с TTL."""
    await cb.answer()
    if cb.message is None or cb.from_user is None or not hasattr(cb.message, "answer"):
        return
    raw = cb.data or ""
    try:
        reward_id = int(raw[len(OTP_CALLBACK_PREFIX) :])
    except (TypeError, ValueError):
        await cb.message.answer(otp_error_text("reward_not_found"))  # type: ignore[union-attr]
        return
    status, body = await _client().request_reward_otp(
        channel_type="telegram",
        external_id=str(cb.from_user.id),
        reward_id=reward_id,
    )
    if status != 200:
        error = str(body.get("error", ""))
        logger.info(
            "my_otp_rejected",
            extra={
                "event": "my_otp_rejected",
                "status": status,
                "reward_id": reward_id,
                "error": error,
            },
        )
        await cb.message.answer(otp_error_text(error))  # type: ignore[union-attr]
        return
    otp = str(body.get("otp", ""))
    ttl = int(body.get("ttl_minutes") or 5)
    # Жирный моноширинный текст: виден с большого экрана, не теряется в чате.
    text = (
        f"Ваш код для погашения:\n<b><code>{otp}</code></b>\n"
        f"Назовите его администратору у стойки. Код действителен {ttl} мин."
    )
    await cb.message.answer(text, parse_mode="HTML")  # type: ignore[union-attr]
    logger.info(
        "my_otp_issued",
        extra={"event": "my_otp_issued", "reward_id": reward_id},
    )
