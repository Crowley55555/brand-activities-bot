from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ButtonSpec(BaseModel):
    text: str
    callback_data: str


class UpdateIn(BaseModel):
    update: dict[str, Any]
    received_at: datetime


class ReplyOut(BaseModel):
    chat_id: str
    text: str
    buttons: list[ButtonSpec] | None = None


class UpdateOut(BaseModel):
    status: Literal["accepted"]
    reply: ReplyOut | None
    is_registered: bool = False


class PiiUpdateIn(BaseModel):
    channel_type: Literal["telegram", "max", "whatsapp", "web"]
    external_id: str
    first_name: str
    last_name: str
    phone: str
    email: str
    policy_version: str


class PiiUpdateOut(BaseModel):
    status: Literal["ok"]
    user_id: int


class ConsentAcceptIn(BaseModel):
    channel_type: Literal["telegram", "max", "whatsapp", "web"]
    external_id: str


class ConsentAcceptOut(BaseModel):
    status: Literal["ok"]
    user_id: int
    policy_version: str


class BrandOut(BaseModel):
    slug: str
    display_name: str
    tagline: str | None = None


class BrandsListOut(BaseModel):
    brands: list[BrandOut]


class ActivityOut(BaseModel):
    slug: str
    display_name: str
    kind: str
    # activities.limits для адаптера (ADR-45): tg_worker берёт из них флаги диалога UGC.
    limits: dict[str, Any]


class ActivitiesListOut(BaseModel):
    activities: list[ActivityOut]


class ActivityStartIn(BaseModel):
    channel_type: Literal["telegram", "max", "whatsapp", "web"]
    external_id: str
    activity_slug: str


class SurveyOptionOut(BaseModel):
    """Вариант ответа для клиента. Вклад в профиль (`profile`) наружу не отдаём."""

    key: str
    label: str


class SurveyQuestionOut(BaseModel):
    id: int
    position: int
    text: str
    question_type: str
    options: list[SurveyOptionOut]


class NextStepOut(BaseModel):
    """Что делает гость дальше. Для UGC - прислать фото; опрос отдаёт `question`."""

    kind: Literal["ask_photo"]


class ActivityStartOut(BaseModel):
    status: Literal["ok"]
    attempt_id: int
    activity_slug: str
    kind: str
    total_questions: int
    question: SurveyQuestionOut | None
    next_step: NextStepOut | None = None


class UgcAcceptIn(BaseModel):
    """Фото и ссылки гостя (фаза A приёмки). Без `tg_file_id` - 422.

    Q1 (ADR-23, фаза 3b): `mime_type` и `file_size_bytes` - декларация из метаданных Telegram.
    Ядро сверяет их с activities.limits (allowed_mimes, max_file_bytes); реальные байты
    проверяет воркер фазы 3c.
    """

    channel_type: Literal["telegram", "max", "whatsapp", "web"]
    external_id: str
    attempt_id: int
    tg_file_id: str = Field(min_length=1, max_length=256)
    mime_type: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
    file_size_bytes: int = Field(ge=1)
    # F3: только https без пробелов, до 2048 символов; javascript:, data:, file:, ftp: - 422.
    post_url: str | None = Field(
        default=None, pattern=r"^https://\S{3,2040}$", max_length=2048
    )
    subscription_url: str | None = Field(
        default=None, pattern=r"^https://\S{3,2040}$", max_length=2048
    )


class UgcAcceptOut(BaseModel):
    status: Literal["ok"]
    submission_id: int


class UgcResubmitIn(UgcAcceptIn):
    """Повтор после отказа: то же тело, что у приёмки (новое фото)."""


class UgcResubmitOut(BaseModel):
    status: Literal["ok"]
    submission_id: int


class ActivityAnswerIn(BaseModel):
    """Канал отправителя нужен, чтобы проверить владельца прохождения (403 attempt_not_owned)."""

    channel_type: Literal["telegram", "max", "whatsapp", "web"]
    external_id: str
    attempt_id: int
    question_id: int
    selected_option_keys: list[str] = Field(default_factory=list)
    free_text: str | None = None


class ActivityAnswerOut(BaseModel):
    status: Literal["ok"]
    attempt_id: int
    is_last: bool
    answered_count: int
    total_questions: int
    next_question: SurveyQuestionOut | None


class ActivityFinishIn(BaseModel):
    channel_type: Literal["telegram", "max", "whatsapp", "web"]
    external_id: str
    attempt_id: int


class ProductCardOut(BaseModel):
    product_name: str
    caption: str | None = None
    product_url: str


class RewardOut(BaseModel):
    """Выданная награда. Тексты карточек - в `recommendations`, здесь только их id (ADR-14)."""

    reward_type: str
    code: str | None = None
    product_recommendation_ids: list[int] | None = None


class ActivityFinishOut(BaseModel):
    status: Literal["ok"]
    attempt_id: int
    summary: str | None
    recommendations: list[ProductCardOut]
    promocode: str | None
    rewards: list[RewardOut]
