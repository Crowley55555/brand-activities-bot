from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    Message,
)

from tg_worker.config import get_worker_settings
from tg_worker.handlers.nav import with_menu
from tg_worker.handlers.states import SurveyStates
from tg_worker.internal_client import InternalApiClient

logger = logging.getLogger("tg_worker.handlers.activity_survey")

router = Router()

# Вид активности из ответа ядра (activities.kind). Slug, названия брендов и опросов
# в tg_worker не живут: их отдаёт ядро из БД (правило 5).
SURVEY_KIND = "survey"
CHANNEL_TYPE = "telegram"
OPTION_PREFIX = "survey_opt:"
DEFAULT_ERROR_TEXT = "Внутренняя ошибка, попробуйте позже."

# Код ошибки ядра -> текст гостю. Тексты адаптера, как в registration.py.
_ERROR_TEXTS: dict[str, str] = {
    "consent_required": "Чтобы пройти опрос, сначала примите политику: /start.",
    "channel_not_found": "Не нашли ваш профиль. Начните заново: /start.",
    "already_passed_today": "Сегодня вы уже проходили этот опрос. Приходите завтра!",
    "already_answered": "Этот вопрос уже отвечен.",
    "already_closed": "Опрос уже завершён.",
    "attempt_not_found": "Опрос не найден. Откройте бренд в меню заново.",
    "attempt_not_owned": "Опрос не найден. Откройте бренд в меню заново.",
    "question_not_found": "Вопрос не найден. Откройте бренд в меню заново.",
    "answer_invalid": "Ответ не принят, попробуйте ещё раз.",
    "survey_incomplete": "Не на все вопросы даны ответы.",
}


def _client() -> InternalApiClient:
    settings = get_worker_settings()
    hmac_key = settings.internal_api_auth_secret
    return InternalApiClient(base_url=settings.internal_api_url, auth_secret=hmac_key)


def _error_text(body: dict[str, Any]) -> str:
    code = body.get("error")
    if isinstance(code, str):
        return _ERROR_TEXTS.get(code, DEFAULT_ERROR_TEXT)
    return DEFAULT_ERROR_TEXT


def pick_survey_activity(activities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Первая активность вида survey из ответа ядра (`slug`, `display_name`, `kind`)."""
    return next((a for a in activities if a.get("kind") == SURVEY_KIND), None)


def _parse_option(data: str) -> tuple[int, str] | None:
    """`survey_opt:<question_id>:<option_key>` -> (question_id, option_key), иначе None."""
    question_part, _, option_key = data.removeprefix(OPTION_PREFIX).partition(":")
    if not question_part.isdigit() or not option_key:
        return None
    return int(question_part), option_key


async def _send_question(message: Message, question: dict[str, Any]) -> None:
    question_id = int(question["id"])
    keyboard = with_menu(
        [
            [
                InlineKeyboardButton(
                    text=str(option["label"]),
                    callback_data=f"{OPTION_PREFIX}{question_id}:{option['key']}",
                )
            ]
            for option in question.get("options") or []
        ]
    )
    await message.answer(f"{question['position']}. {question['text']}", reply_markup=keyboard)


async def _send_result(message: Message, body: dict[str, Any]) -> None:
    """Карточки продуктов (название, подпись, ссылка) и одно сообщение с промокодом."""
    cards = body.get("recommendations") or []
    texts: list[str] = []
    for card in cards:
        lines = [str(card.get("product_name") or "")]
        if card.get("caption"):
            lines.append(str(card["caption"]))
        lines.append(str(card.get("product_url") or ""))
        texts.append("\n".join(lines))
    promocode = body.get("promocode")
    if promocode:
        texts.append(f"Ваш промокод: {promocode}")
    elif not cards:
        texts.append("Спасибо за прохождение!")
    # «⌂ В меню» - на последнем сообщении итога.
    for index, text in enumerate(texts):
        if index == len(texts) - 1:
            await message.answer(text, reply_markup=with_menu())
        else:
            await message.answer(text)


async def _finish(
    message: Message, state: FSMContext, attempt_id: int, external_id: str
) -> None:
    # Состояние сбрасываем до запроса: повторный клик не запустит второе закрытие.
    await state.clear()
    try:
        status, body = await _client().finish_activity(CHANNEL_TYPE, external_id, attempt_id)
    except Exception:
        logger.exception("survey_finish_failed")
        await message.answer(DEFAULT_ERROR_TEXT)
        return
    if status != 200:
        logger.warning(
            "survey_finish_rejected",
            extra={
                "event": "survey_finish_rejected",
                "status": status,
                "error": body.get("error"),
            },
        )
        await message.answer(_error_text(body))
        return
    # Промокод в лог не пишем: это предъявительский код.
    await _send_result(message, body)


async def launch_survey(cb: CallbackQuery, state: FSMContext, activity: dict[str, Any]) -> None:
    """Старт опроса: ядро -> первый вопрос с inline-кнопками. Ошибки -> текст гостю."""
    if cb.message is None:
        return
    message = cb.message
    try:
        status, body = await _client().start_activity(
            CHANNEL_TYPE, str(cb.from_user.id), str(activity["slug"])
        )
    except Exception:
        logger.exception("survey_start_failed")
        await message.answer(DEFAULT_ERROR_TEXT)
        return
    question = body.get("question")
    if status != 200 or not isinstance(question, dict):
        logger.warning(
            "survey_start_rejected",
            extra={
                "event": "survey_start_rejected",
                "status": status,
                "error": body.get("error"),
            },
        )
        await message.answer(_error_text(body))
        return
    await state.set_state(SurveyStates.WaitSurveyAnswer)
    await state.update_data(attempt_id=body["attempt_id"], question_id=question["id"])
    await message.answer(
        f"Опрос «{activity.get('display_name', '')}», вопросов: {body['total_questions']}."
    )
    await _send_question(message, question)


@router.callback_query(SurveyStates.WaitSurveyAnswer, F.data.startswith(OPTION_PREFIX))
async def handle_survey_option(cb: CallbackQuery, state: FSMContext) -> None:
    """Ответ на текущий вопрос -> ядро -> следующий вопрос либо финал (`is_last`)."""
    data = await state.get_data()
    parsed = _parse_option(cb.data or "")
    attempt_id = data.get("attempt_id")
    if parsed is None or attempt_id is None or parsed[0] != data.get("question_id"):
        # Кнопка уже отвеченного вопроса (двойной клик или старое сообщение).
        await cb.answer("Этот вопрос уже отвечен.")
        return
    question_id, option_key = parsed
    await cb.answer()
    if cb.message is None:
        return
    message = cb.message
    try:
        status, body = await _client().answer_activity(
            CHANNEL_TYPE, str(cb.from_user.id), int(attempt_id), question_id, [option_key]
        )
    except Exception:
        logger.exception("survey_answer_failed")
        await message.answer(DEFAULT_ERROR_TEXT)
        return
    if status != 200:
        logger.warning(
            "survey_answer_rejected",
            extra={
                "event": "survey_answer_rejected",
                "status": status,
                "error": body.get("error"),
            },
        )
        await message.answer(_error_text(body))
        return
    if body.get("is_last"):
        await _finish(message, state, int(attempt_id), str(cb.from_user.id))
        return
    next_question = body.get("next_question")
    if not isinstance(next_question, dict):
        await state.clear()
        await message.answer(DEFAULT_ERROR_TEXT)
        return
    await state.update_data(question_id=next_question["id"])
    await _send_question(message, next_question)


@router.callback_query(F.data.startswith(OPTION_PREFIX))
async def handle_stale_survey_option(cb: CallbackQuery) -> None:
    """Кнопка опроса, который уже не ведётся (завершён или состояние сброшено)."""
    await cb.answer("Опрос уже завершён. Откройте бренд в меню заново.")
