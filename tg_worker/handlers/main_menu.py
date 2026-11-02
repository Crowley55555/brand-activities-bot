from __future__ import annotations

import logging
from typing import Any, Final

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from tg_worker.config import get_worker_settings
from tg_worker.handlers.activity_survey import SURVEY_KIND, launch_survey
from tg_worker.handlers.booking import BOOKING_KIND, show_booking_slots
from tg_worker.handlers.confirmation import CONFIRMATION_KIND, launch_confirmation
from tg_worker.handlers.photo_frame import PHOTO_FRAME_KIND, launch_photo_frame
from tg_worker.handlers.ugc import UGC_KIND, launch_ugc
from tg_worker.handlers.wheel import WHEEL_KIND, spin_wheel
from tg_worker.internal_client import InternalApiClient

logger = logging.getLogger("tg_worker.handlers.main_menu")

router = Router()

ACTIVITY_CALLBACK_PREFIX: Final = "activity:"
# Активности выбранного бренда (ответ ядра) лежат в FSM до клика по кнопке активности.
BRAND_ACTIVITIES_KEY: Final = "brand_activities"


def _client() -> InternalApiClient:
    settings = get_worker_settings()
    hmac_key = settings.internal_api_auth_secret
    return InternalApiClient(
        base_url=settings.internal_api_url, auth_secret=hmac_key
    )


async def show_main_menu(message: Message) -> None:
    """Рисует меню из 5 брендов, прочитанных из ядра."""
    client = _client()
    try:
        brands = await client.list_brands()
    except Exception:
        logger.exception("brands_fetch_failed")
        await message.answer("Не получилось загрузить список брендов.")
        return

    if not brands:
        await message.answer("Активных брендов пока нет.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=str(b.get("display_name") or b.get("slug") or "?"),
                    callback_data=f"brand:{b.get('slug')}",
                )
            ]
            for b in brands
        ]
    )
    await message.answer("Выберите бренд:", reply_markup=keyboard)


async def _launch_activity(cb: CallbackQuery, state: FSMContext, activity: dict[str, Any]) -> None:
    """Запускает активность по её виду из ядра: опрос, запись, колесо, фото или ссылка на пост."""
    kind = activity.get("kind")
    if kind == SURVEY_KIND:
        await launch_survey(cb, state, activity)
        return
    if kind == BOOKING_KIND:
        await show_booking_slots(cb, state, str(activity.get("slug")))
        return
    if kind == WHEEL_KIND:
        await spin_wheel(cb, state, activity)
        return
    if kind == PHOTO_FRAME_KIND:
        await launch_photo_frame(cb, state, activity)
        return
    if kind == CONFIRMATION_KIND:
        await launch_confirmation(cb, state, activity)
        return
    if kind == UGC_KIND:
        await launch_ugc(cb, state, activity)
        return
    if cb.message is not None:
        await cb.message.answer("Эта активность пока недоступна в боте.")


@router.callback_query(F.data.startswith("brand:"))
async def handle_brand_click(cb: CallbackQuery, state: FSMContext) -> None:
    """Клик по бренду (ревью фазы 4, B1): одна активность - сразу в неё, иначе кнопка на каждую.

    У бренда могут быть опрос и запись одновременно (Бренд A). Прямой переход на первую
    активность скрывал бы вторую, поэтому при нескольких активностях показываем все.
    """
    data = cb.data or ""
    slug = data.split(":", 1)[1] if ":" in data else "?"
    await cb.answer()
    if cb.message is None:
        return
    try:
        activities = await _client().list_brand_activities(slug)
    except Exception:
        logger.exception("brand_activities_fetch_failed")
        await cb.message.answer("Не получилось загрузить активности бренда.")
        return
    if not activities:
        await cb.message.answer(f"Активность бренда {slug} будет позже.")
        return
    if len(activities) == 1:
        await _launch_activity(cb, state, activities[0])
        return
    await state.update_data(**{BRAND_ACTIVITIES_KEY: activities})
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=str(a.get("display_name") or a.get("slug") or "?"),
                    callback_data=f"{ACTIVITY_CALLBACK_PREFIX}{a.get('slug')}",
                )
            ]
            for a in activities
        ]
    )
    await cb.message.answer("Выберите активность:", reply_markup=keyboard)


@router.callback_query(F.data.startswith(ACTIVITY_CALLBACK_PREFIX))
async def handle_activity_click(cb: CallbackQuery, state: FSMContext) -> None:
    """Клик по кнопке активности: вид берём из списка активностей бренда, сохранённого в FSM."""
    await cb.answer()
    if cb.message is None:
        return
    slug = (cb.data or "")[len(ACTIVITY_CALLBACK_PREFIX) :]
    stored: list[dict[str, Any]] = (await state.get_data()).get(BRAND_ACTIVITIES_KEY) or []
    activity = next((a for a in stored if a.get("slug") == slug), None)
    if activity is None:
        await cb.message.answer("Выберите бренд заново.")
        return
    await _launch_activity(cb, state, activity)
