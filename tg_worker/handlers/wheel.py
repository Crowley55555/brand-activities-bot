"""Колесо момента в Telegram (ADR-39): запуск из меню активности kind='wheel' - один спин.

Результат (приз и код промокода) приходит отдельным сообщением из outbox (шаблоны wheel_result и
promocode), поэтому здесь подтверждаем запуск и переводим ошибки ядра в текст. Бот не ходит в БД
(правило 3): slug и ответы приходят из ядра.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from tg_worker.config import get_worker_settings
from tg_worker.handlers.nav import with_menu
from tg_worker.internal_client import InternalApiClient

logger = logging.getLogger("tg_worker.handlers.wheel")

WHEEL_KIND: Final[str] = "wheel"


def _client() -> InternalApiClient:
    settings = get_worker_settings()
    hmac_key = settings.internal_api_auth_secret
    return InternalApiClient(base_url=settings.internal_api_url, auth_secret=hmac_key)


def wheel_error_text(error: str) -> str:
    """Ошибка ядра -> текст гостю. Неизвестная ошибка - общий ответ, без внутренних кодов."""
    if error == "already_spun_today":
        return "Сегодня вы уже крутили колесо. Попробуйте завтра."
    if error == "already_passed_today":
        return "Сегодня эта активность уже пройдена."
    if error == "prizes_out_of_stock":
        return "Призы на сегодня закончились."
    if error == "consent_required":
        return "Чтобы крутить колесо, сначала подтвердите согласие: /start."
    return "Не получилось крутануть колесо. Попробуйте позже."


async def spin_wheel(cb: CallbackQuery, state: FSMContext, activity: dict[str, Any]) -> None:
    """Один спин по выбранной активности; успех подтверждаем, результат придёт из outbox."""
    if cb.message is None:
        return
    await state.clear()
    status, body = await _client().wheel_spin(
        channel_type="telegram",
        external_id=str(cb.from_user.id),
        activity_slug=str(activity.get("slug")),
    )
    if status == 200:
        await cb.message.answer(
            "Колесо крутится! Результат придёт отдельным сообщением.", reply_markup=with_menu()
        )
        return
    logger.info("wheel_spin_rejected", extra={"event": "wheel_spin_rejected", "status": status})
    await cb.message.answer(wheel_error_text(str(body.get("error", ""))))
