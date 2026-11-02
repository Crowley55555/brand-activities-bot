"""Запись на слот в Telegram (ADR-37): активность вида booking любого бренда.

Поток: бренд -> активность kind='booking' из ядра -> свободные слоты на сегодня -> кнопка слота
`slot:<id>:<start>` -> POST /book. FSM BookingStates.WaitSlotChoice держит slug активности между
экранами. Сам бот не ходит в БД (правило 3): slug, список и ответы приходят из ядра. Ответы 409
(slot_taken, already_passed_today, consent_required) переводятся в понятный текст.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from tg_worker.config import get_worker_settings
from tg_worker.handlers.nav import BRANDS_TEXT, NAV_MENU, back_button, with_menu
from tg_worker.handlers.states import BookingStates
from tg_worker.internal_client import InternalApiClient

logger = logging.getLogger("tg_worker.handlers.booking")

BOOKING_KIND = "booking"
# Часовой пояс отображения времени слота (событие идёт по Москве; канон 03, attempt_date).
DISPLAY_TIMEZONE = "Europe/Moscow"

router = Router()


def _client() -> InternalApiClient:
    settings = get_worker_settings()
    hmac_key = settings.internal_api_auth_secret
    return InternalApiClient(base_url=settings.internal_api_url, auth_secret=hmac_key)


def _time_label(start_at: str) -> str:
    return datetime.fromisoformat(start_at).astimezone(ZoneInfo(DISPLAY_TIMEZONE)).strftime("%H:%M")


def pick_booking_activity(activities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Первая активность вида booking из ответа ядра (`slug`, `kind`)."""
    return next((a for a in activities if a.get("kind") == BOOKING_KIND), None)


async def show_booking_slots(cb: CallbackQuery, state: FSMContext, activity_slug: str) -> None:
    """Список свободных слотов на сегодня кнопками; slug активности - в FSM."""
    if cb.message is None:
        return
    status, body = await _client().booking_list_slots(
        channel_type="telegram",
        external_id=str(cb.from_user.id),
        activity_slug=activity_slug,
    )
    if status != 200:
        logger.warning(
            "booking_slots_failed", extra={"event": "booking_slots_failed", "status": status}
        )
        await cb.message.answer("Не получилось загрузить свободное время.")
        return
    slots = body.get("slots") or []
    if not slots:
        await cb.message.answer("На сегодня свободных слотов нет.")
        return
    await state.set_state(BookingStates.WaitSlotChoice)
    await state.update_data(activity_slug=activity_slug)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_time_label(str(slot["start_at"])),
                    callback_data=f"slot:{slot['slot_id']}:{slot['start_at']}",
                )
            ]
            for slot in slots
        ]
        + [[back_button(NAV_MENU, BRANDS_TEXT)]]
    )
    await cb.message.answer("Выберите свободное время на сегодня:", reply_markup=keyboard)


@router.callback_query(StateFilter(BookingStates.WaitSlotChoice), F.data.startswith("slot:"))
async def handle_slot_click(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if cb.message is None:
        return
    try:
        slot_id = int((cb.data or "").split(":", 2)[1])
    except (IndexError, ValueError):
        await cb.message.answer("Не удалось распознать слот. Выберите время ещё раз.")
        return
    data = await state.get_data()
    activity_slug = str(data.get("activity_slug", ""))
    status, body = await _client().booking_book(
        channel_type="telegram",
        external_id=str(cb.from_user.id),
        activity_slug=activity_slug,
        slot_id=slot_id,
    )
    if status == 200:
        await state.clear()
        text = f"Вы записаны на {_time_label(str(body['slot_start']))}."
        if body.get("has_reminders"):
            text += f" Напоминание придёт за {int(body['remind_before_minutes'])} мин."
        await cb.message.answer(text, reply_markup=with_menu())
        return
    error = str(body.get("error", ""))
    if error == "slot_taken":
        await cb.message.answer("Этот слот только что заняли. Вот свежий список:")
        await show_booking_slots(cb, state, activity_slug)
        return
    await state.clear()
    if error == "already_passed_today":
        await cb.message.answer("Вы уже записывались сегодня.")
    elif error == "consent_required":
        await cb.message.answer("Чтобы записаться, сначала подтвердите согласие: /start.")
    else:
        await cb.message.answer("Не получилось записать. Попробуйте позже.")
