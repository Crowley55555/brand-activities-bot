from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ConsentStates(StatesGroup):
    WaitConsent = State()


class RegistrationStates(StatesGroup):
    WaitFirstName = State()
    WaitLastName = State()
    WaitPhone = State()
    WaitEmail = State()


class BookingStates(StatesGroup):
    """Выбор слота записи. `activity_slug` лежит в FSM data (из ответа ядра)."""

    WaitSlotChoice = State()


class SurveyStates(StatesGroup):
    """Опрос активности. `attempt_id` и `question_id` лежат в FSM data (ответ ядра)."""

    WaitSurveyAnswer = State()


class PhotoFrameStates(StatesGroup):
    """Ожидание фото для рамки Polaroid. `activity_slug` лежит в FSM data (из меню)."""

    WaitPhoto = State()


class ConfirmationStates(StatesGroup):
    """Ожидание ссылки на пост (подарок за пост). `activity_slug` лежит в FSM data."""

    WaitPostLink = State()


class UgcStates(StatesGroup):
    """Фото-активность (UGC): фото, затем ссылка на пост и ссылка на подписку, если нужны.

    В FSM data лежат `activity_slug`, `attempt_id` (из ответа ядра), флаги `requires_post_url` и
    `requires_subscription_url` (из activities.limits), затем фото и введённые ссылки.
    """

    WaitPhoto = State()
    WaitPostUrl = State()
    WaitSubscriptionUrl = State()


class ForgetStates(StatesGroup):
    """`/forget` двойное подтверждение (фаза 10b-3, I-2).

    Канон 02 §9: гость набирает `/forget`, бот предупреждает и ждёт ровно
    «ПОДТВЕРЖДАЮ». Любой другой текст / `/cancel` сбрасывает FSM.
    """

    WaitConfirm = State()
