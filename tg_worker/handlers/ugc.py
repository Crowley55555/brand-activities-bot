"""Фото-активности (UGC) в Telegram: фото и, если активность просит, ссылки (known-limit B-1).

Ядро открывает прохождение (`/activities/start`) и принимает материал (`/activities/ugc/accept`);
адаптер ведёт диалог: фото -> ссылка на пост -> ссылка на подписку -> accept. Что просить кроме
фото, решают флаги `requires_post_url` и `requires_subscription_url` из `activities.limits`
(правило 5): их несёт словарь активности (`activity["limits"]`), slug'ов и брендов здесь нет.
Если `limits` в словаре нет, что просить у гостя неизвестно: прохождение не открываем и отвечаем,
что активность пока недоступна (как для неизвестных видов).

Согласие, лимиты файла, MIME, модерация и уведомления - в ядре (правила 1, 3), бот не ходит в БД.
В лог не пишем ссылки и tg_file_id (правило 2): только статусы и коды ошибок.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup, Message

from tg_worker.config import get_worker_settings
from tg_worker.handlers.nav import NAV_UGC_BACK, back_button, with_menu
from tg_worker.handlers.states import UgcStates
from tg_worker.internal_client import InternalApiClient

logger = logging.getLogger("tg_worker.handlers.ugc")

UGC_KIND: Final[str] = "ugc"
CHANNEL_TYPE: Final[str] = "telegram"
# Telegram перекодирует фото в JPEG; лимиты MIME и размера проверяет ядро по activities.limits.
TELEGRAM_PHOTO_MIME: Final[str] = "image/jpeg"

# Граница ссылок та же, что у UgcAcceptIn в ядре: только https, без пробелов, до 2048 символов.
# Иначе ядро ответило бы 422 уже после того, как гость прислал фото и обе ссылки.
_HTTPS_PREFIX: Final[str] = "https://"
_URL_RE: Final[re.Pattern[str]] = re.compile(r"https://\S{3,2040}")

ASK_PHOTO: Final[str] = "Пришлите фото."
ASK_POST_URL: Final[str] = "Пришлите ссылку на пост."
ASK_SUBSCRIPTION_URL: Final[str] = "Пришлите ссылку на профиль подписки."
HINT_PHOTO: Final[str] = "Нужно фото. Пришлите его как фото, не файлом."
HINT_POST_URL: Final[str] = "Пришлите ссылку на пост, она должна начинаться с https://"
HINT_SUBSCRIPTION_URL: Final[str] = (
    "Пришлите ссылку на профиль подписки, она должна начинаться с https://"
)
ACCEPTED_TEXT: Final[str] = "Фото принято, модератор проверит в течение дня."
UNAVAILABLE_TEXT: Final[str] = "Эта активность пока недоступна в боте."
START_FAILED_TEXT: Final[str] = "Не получилось начать. Попробуйте позже."
ACCEPT_FAILED_TEXT: Final[str] = "Не получилось принять фото. Попробуйте позже."
RETRY_PHOTO_TEXT: Final[str] = "Не получилось принять фото. Пришлите его ещё раз."
_NEW_PHOTO_TEXT: Final[str] = "Фото не подходит. Пришлите другое фото."
_REOPEN_TEXT: Final[str] = "Заявка уже отправлена или закрыта. Откройте активность в меню заново."

# Код ошибки ядра -> текст гостю. Внутренние коды наружу не уходят, неизвестный код - общий текст.
_ERROR_TEXTS: Final[dict[str, str]] = {
    "consent_required": "Чтобы прислать фото, подтвердите согласие: /start.",
    "channel_not_found": "Не нашли ваш профиль. Начните заново: /start.",
    "already_passed_today": "Сегодня вы уже участвовали в этой активности. Приходите завтра!",
    "activity_not_found": "Эта активность сейчас недоступна.",
    "file_too_large": "Фото слишком большое. Пришлите другое фото.",
    "mime_not_allowed": _NEW_PHOTO_TEXT,
    "file_size_invalid": _NEW_PHOTO_TEXT,
    "tg_file_id_required": _NEW_PHOTO_TEXT,
    "bad_request": "Не получилось принять фото или ссылки. Пришлите фото ещё раз.",
    "attempt_not_found": _REOPEN_TEXT,
    "attempt_not_owned": _REOPEN_TEXT,
    "attempt_not_in_progress": _REOPEN_TEXT,
}
# Ответы /start, которые гостю есть смысл объяснять; остальное - «не получилось начать».
_START_ERRORS: Final[frozenset[str]] = frozenset(
    {"consent_required", "channel_not_found", "already_passed_today", "activity_not_found"}
)
# Отказ приёмки до любой записи: прохождение осталось открытым, гость может прислать другое фото.
# Сбросить FSM здесь нельзя: открытое прохождение держит «раз в день», повторный старт даст 409.
_FIXABLE_WITH_NEW_PHOTO: Final[frozenset[str]] = frozenset(
    {
        "file_too_large",
        "mime_not_allowed",
        "file_size_invalid",
        "tg_file_id_required",
        "bad_request",
    }
)


def _name(step: State) -> str:
    return step.state or ""


_ASK_BY_STATE: Final[dict[str, str]] = {
    _name(UgcStates.WaitPhoto): ASK_PHOTO,
    _name(UgcStates.WaitPostUrl): ASK_POST_URL,
    _name(UgcStates.WaitSubscriptionUrl): ASK_SUBSCRIPTION_URL,
}
_HINT_BY_STATE: Final[dict[str, str]] = {
    _name(UgcStates.WaitPhoto): HINT_PHOTO,
    _name(UgcStates.WaitPostUrl): HINT_POST_URL,
    _name(UgcStates.WaitSubscriptionUrl): HINT_SUBSCRIPTION_URL,
}

router = Router()


def _ask_markup(step: str) -> InlineKeyboardMarkup:
    """Клавиатура шага: «← Назад» на шагах со ссылками, «⌂ В меню» везде."""
    if step == _name(UgcStates.WaitPhoto):
        return with_menu()
    return with_menu([[back_button(NAV_UGC_BACK)]])


def _client() -> InternalApiClient:
    settings = get_worker_settings()
    hmac_key = settings.internal_api_auth_secret
    return InternalApiClient(base_url=settings.internal_api_url, auth_secret=hmac_key)


def ugc_error_text(error: str, *, fallback: str = ACCEPT_FAILED_TEXT) -> str:
    return _ERROR_TEXTS.get(error, fallback)


def normalize_url(raw: str | None) -> str | None:
    """Ссылка в том виде, в каком её примет ядро, либо None.

    Пробелы по краям срезаются, схема приводится к нижнему регистру: мобильная клавиатура
    пишет «Https://» с заглавной, а ядро сверяет схему как есть.
    """
    if raw is None:
        return None
    text = raw.strip()
    if text[: len(_HTTPS_PREFIX)].lower() == _HTTPS_PREFIX:
        text = _HTTPS_PREFIX + text[len(_HTTPS_PREFIX) :]
    return text if _URL_RE.fullmatch(text) else None


def _read_requirements(activity: dict[str, Any]) -> tuple[bool, bool] | None:
    """(нужна ссылка на пост, нужна ссылка на подписку) из `activity["limits"]`.

    Нет ключа - ссылка не нужна. None - просить у гостя неизвестно что: ядро не передало `limits`
    либо флаг записан не как boolean (строка «true» молча стала бы «не нужна», и модератор
    остался бы без ссылок). В обоих случаях прохождение не открываем.
    """
    limits = activity.get("limits")
    if not isinstance(limits, dict):
        return None
    post = limits.get("requires_post_url", False)
    subscription = limits.get("requires_subscription_url", False)
    if not isinstance(post, bool) or not isinstance(subscription, bool):
        return None
    return post, subscription


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


async def _remind_current_step(
    message: Message | InaccessibleMessage, state: FSMContext, slug: str
) -> bool:
    """Повторный клик по активности, которую гость уже ведёт: напоминаем текущий шаг.

    Открытое прохождение держит «раз в день», поэтому 409 на втором тапе - не итог, а признак
    двойного клика. Сброс FSM оставил бы начатое прохождение без хозяина до истечения срока.
    """
    ask = _ASK_BY_STATE.get(await state.get_state() or "")
    data = await state.get_data()
    if ask is None or data.get("activity_slug") != slug or data.get("attempt_id") is None:
        return False
    await message.answer(ask, reply_markup=_ask_markup(await state.get_state() or ""))
    return True


async def launch_ugc(cb: CallbackQuery, state: FSMContext, activity: dict[str, Any]) -> None:
    """Старт из меню: ядро открывает прохождение, гость присылает фото."""
    if cb.message is None:
        return
    message = cb.message
    slug = str(activity.get("slug", ""))
    requirements = _read_requirements(activity)
    if requirements is None:
        logger.warning(
            "ugc_requirements_unknown",
            extra={"event": "ugc_requirements_unknown", "activity_slug": slug},
        )
        await message.answer(UNAVAILABLE_TEXT)
        return
    try:
        status, body = await _client().start_activity(
            channel_type=CHANNEL_TYPE, external_id=str(cb.from_user.id), activity_slug=slug
        )
    except Exception:
        logger.exception("ugc_start_failed")
        await message.answer(START_FAILED_TEXT)
        return
    attempt_id = body.get("attempt_id")
    if status != 200 or not _is_int(attempt_id):
        error = str(body.get("error", ""))
        logger.info(
            "ugc_start_rejected",
            extra={"event": "ugc_start_rejected", "status": status, "error": error},
        )
        if error == "already_passed_today" and await _remind_current_step(message, state, slug):
            return
        await state.clear()
        await message.answer(ugc_error_text(error) if error in _START_ERRORS else START_FAILED_TEXT)
        return
    requires_post_url, requires_subscription_url = requirements
    await state.set_state(UgcStates.WaitPhoto)
    await state.update_data(
        activity_slug=slug,
        attempt_id=attempt_id,
        requires_post_url=requires_post_url,
        requires_subscription_url=requires_subscription_url,
        post_url=None,
        subscription_url=None,
        tg_file_id=None,
        mime_type=None,
        file_size_bytes=None,
    )
    await message.answer(ASK_PHOTO, reply_markup=_ask_markup(_name(UgcStates.WaitPhoto)))


async def _back_to_photo(state: FSMContext) -> None:
    """Прохождение открыто: просим новое фото, собранное вместе со старым фото сбрасываем."""
    await state.set_state(UgcStates.WaitPhoto)
    await state.update_data(
        post_url=None,
        subscription_url=None,
        tg_file_id=None,
        mime_type=None,
        file_size_bytes=None,
    )


async def _submit(message: Message, state: FSMContext) -> None:
    """Отправка собранного в ядро.

    Состояние ожидания снимаем до запроса: следующий апдейт альбома не попадёт в тот же шаг и не
    запустит второй accept (ядро ответило бы ложным attempt_not_in_progress).
    """
    if message.from_user is None:
        return
    data = await state.get_data()
    await state.set_state(None)
    try:
        status, body = await _client().ugc_accept(
            channel_type=CHANNEL_TYPE,
            external_id=str(message.from_user.id),
            attempt_id=int(data["attempt_id"]),
            tg_file_id=str(data["tg_file_id"]),
            mime_type=str(data["mime_type"]),
            file_size_bytes=int(data["file_size_bytes"]),
            post_url=data.get("post_url"),
            subscription_url=data.get("subscription_url"),
        )
    except Exception:
        logger.exception("ugc_accept_failed")
        await _back_to_photo(state)
        await message.answer(RETRY_PHOTO_TEXT)
        return
    if status == 200:
        await state.clear()
        await message.answer(ACCEPTED_TEXT, reply_markup=with_menu())
        return
    error = str(body.get("error", ""))
    logger.info(
        "ugc_accept_rejected",
        extra={"event": "ugc_accept_rejected", "status": status, "error": error},
    )
    if error in _FIXABLE_WITH_NEW_PHOTO:
        await _back_to_photo(state)
        await message.answer(ugc_error_text(error))
    elif status >= 500:
        await _back_to_photo(state)
        await message.answer(RETRY_PHOTO_TEXT)
    else:
        await state.clear()
        await message.answer(ugc_error_text(error))


async def _advance(message: Message, state: FSMContext) -> None:
    """Следующий шаг по флагам активности: недостающая ссылка либо отправка в ядро."""
    data = await state.get_data()
    if data.get("requires_post_url") and not data.get("post_url"):
        step = UgcStates.WaitPostUrl
    elif data.get("requires_subscription_url") and not data.get("subscription_url"):
        step = UgcStates.WaitSubscriptionUrl
    else:
        await _submit(message, state)
        return
    await state.set_state(step)
    await message.answer(_ASK_BY_STATE[_name(step)], reply_markup=_ask_markup(_name(step)))


@router.message(StateFilter(UgcStates.WaitPhoto), F.photo)
async def handle_photo(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.photo:
        return
    largest = message.photo[-1]
    if not largest.file_size:
        # Размер нужен ядру для проверки лимита: без него фото не отправляем.
        await message.answer(ugc_error_text("file_size_invalid"))
        return
    await state.update_data(
        tg_file_id=largest.file_id,
        mime_type=TELEGRAM_PHOTO_MIME,
        file_size_bytes=largest.file_size,
    )
    await _advance(message, state)


@router.message(StateFilter(UgcStates.WaitPostUrl), F.text)
async def handle_post_url(message: Message, state: FSMContext) -> None:
    url = normalize_url(message.text)
    if url is None:
        await message.answer(HINT_POST_URL)
        return
    await state.update_data(post_url=url)
    await _advance(message, state)


@router.message(StateFilter(UgcStates.WaitSubscriptionUrl), F.text)
async def handle_subscription_url(message: Message, state: FSMContext) -> None:
    url = normalize_url(message.text)
    if url is None:
        await message.answer(HINT_SUBSCRIPTION_URL)
        return
    await state.update_data(subscription_url=url)
    await _advance(message, state)


@router.message(
    StateFilter(UgcStates.WaitPhoto, UgcStates.WaitPostUrl, UgcStates.WaitSubscriptionUrl)
)
async def handle_unexpected(message: Message, state: FSMContext) -> None:
    """Ввод не по шагу (текст вместо фото, фото вместо ссылки): подсказка. Альбом - молча.

    Регистрируется последним: сюда попадает только то, что не взяли обработчики шагов.
    """
    if message.media_group_id is not None:
        return
    hint = _HINT_BY_STATE.get(await state.get_state() or "")
    if hint is not None:
        await message.answer(hint)


@router.callback_query(
    StateFilter(UgcStates.WaitPostUrl, UgcStates.WaitSubscriptionUrl), F.data == NAV_UGC_BACK
)
async def handle_back(cb: CallbackQuery, state: FSMContext) -> None:
    """«← Назад»: ссылка на подписку -> ссылка на пост; ссылка на пост -> фото.

    Прохождение в ядре остаётся открытым (нового /start нет), сбрасываем только введённое.
    Если ссылки на пост у активности нет, из подписки возвращаемся сразу к фото.
    """
    await cb.answer()
    if cb.message is None:
        return
    data = await state.get_data()
    current = await state.get_state()
    logger.info("nav_back", extra={"event": "nav_back", "activity_slug": data.get("activity_slug")})
    if current == _name(UgcStates.WaitSubscriptionUrl) and data.get("requires_post_url"):
        await state.update_data(subscription_url=None)
        await state.set_state(UgcStates.WaitPostUrl)
        await cb.message.answer(
            ASK_POST_URL, reply_markup=_ask_markup(_name(UgcStates.WaitPostUrl))
        )
        return
    await _back_to_photo(state)
    await cb.message.answer(ASK_PHOTO, reply_markup=_ask_markup(_name(UgcStates.WaitPhoto)))
