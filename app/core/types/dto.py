from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.core.types.enums import (
    ActivityKind,
    AttemptStatus,
    ChannelType,
    QuestionType,
    RewardType,
    UgcStatus,
)
from app.core.types.ids import (
    ActivityId,
    AdminId,
    AttemptId,
    BrandId,
    ChannelAccountId,
    ProductRecommendationId,
    RewardId,
    SurveyQuestionId,
    UgcSubmissionId,
    UserId,
)


@dataclass(frozen=True)
class UserRef:
    id: UserId
    is_registered: bool
    anonymized_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class UserListRow:
    """Строка списка /admin/users. ПДн сырые: маскирует UI-слой."""

    id: UserId
    created_at: datetime
    registered_at: datetime | None
    last_seen_at: datetime | None
    anonymized_at: datetime | None
    first_name: str | None
    last_name: str | None
    phone: str | None
    email: str | None

    @property
    def is_anonymized(self) -> bool:
        return self.anonymized_at is not None


@dataclass(frozen=True)
class UserPii:
    id: UserId
    first_name: str | None
    last_name: str | None
    phone: str | None
    email: str | None


@dataclass(frozen=True)
class ChannelRef:
    id: ChannelAccountId
    user_id: UserId
    channel_type: ChannelType
    external_id: str


@dataclass(frozen=True)
class BrandRef:
    id: BrandId
    slug: str
    display_name: str
    tagline: str | None


@dataclass(frozen=True)
class AdminRef:
    """Сотрудник админки без секретов: хэш пароля сюда не попадает."""

    id: AdminId
    login: str
    display_name: str


@dataclass(frozen=True)
class ActivityRef:
    """Активность бренда (справочник). `limits` - настройки из activities.limits (JSONB)."""

    id: ActivityId
    brand_id: BrandId
    slug: str
    display_name: str
    kind: ActivityKind
    limits: Mapping[str, Any]


@dataclass(frozen=True)
class AttemptRef:
    """Прохождение активности гостем. ПДн сюда не попадают (summary - только слаги/коды)."""

    id: AttemptId
    user_id: UserId
    activity_id: ActivityId
    activity_slug: str
    channel_account_id: ChannelAccountId
    status: AttemptStatus
    started_at: datetime
    attempt_date: date
    finished_at: datetime | None = None
    summary: str | None = None


@dataclass(frozen=True)
class SurveyOptionRef:
    """Вариант ответа. `profile` - вклад варианта в профиль гостя (атрибут -> значение)."""

    key: str
    label: str
    profile: Mapping[str, str]


@dataclass(frozen=True)
class SurveyQuestionRef:
    id: SurveyQuestionId
    activity_id: ActivityId
    position: int
    text: str
    question_type: QuestionType
    options: tuple[SurveyOptionRef, ...]


@dataclass(frozen=True)
class SurveyAnswerRef:
    attempt_id: AttemptId
    question_id: SurveyQuestionId
    selected_option_keys: tuple[str, ...]
    free_text: str | None
    answered_at: datetime


@dataclass(frozen=True)
class ProductRecommendationRef:
    """Карточка продукта. Тексты и URL актуальные: награда хранит только id (ADR-14)."""

    id: ProductRecommendationId
    activity_id: ActivityId
    condition: Mapping[str, str]
    product_name: str
    caption: str | None
    product_url: str
    position: int


@dataclass(frozen=True)
class GrantSpec:
    """Что выдать за завершённое прохождение (04 §4.4, 08 §4.1)."""

    reward_type: RewardType
    title: str | None = None
    code_template: str | None = None  # шаблон вида '<ПРЕФИКС>-{random8}'
    product_recommendation_ids: Sequence[int] | None = None
    wheel_prize_id: int | None = None


@dataclass(frozen=True)
class RewardListRow:
    """Строка списка /admin/rewards. Без ПДн: гость обозначен только внутренним user_id."""

    id: int
    issued_at: datetime
    reward_type: str
    code: str | None
    title: str | None
    attempt_id: int
    user_id: int
    activity_slug: str
    activity_display_name: str
    brand_slug: str
    redeemed_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class RewardIssued:
    """Выданная награда.

    `recommendations` заполняет только `core.rewards.list_by_user`: актуальные карточки
    по сохранённым id, включая soft-deleted (ADR-14).
    """

    id: RewardId
    attempt_id: AttemptId
    reward_type: RewardType
    code: str | None
    title: str | None
    issued_at: datetime
    product_recommendation_ids: tuple[int, ...] | None = None
    expires_at: datetime | None = None
    redeemed_at: datetime | None = None
    revoked_at: datetime | None = None
    recommendations: tuple[ProductRecommendationRef, ...] = ()


@dataclass(frozen=True)
class UgcSubmissionRef:
    """Фото и ссылки гостя (дочь прохождения). Файл - только ссылка в хранилище, не байты.

    ПДн сюда не попадают: `post_url`/`subscription_claimed_url` - ссылки на публикации.
    """

    id: UgcSubmissionId
    attempt_id: AttemptId
    status: UgcStatus
    post_url: str | None
    subscription_claimed_url: str | None
    file_bucket: str | None
    file_key: str | None
    file_content_type: str | None
    file_size_bytes: int | None
    rejection_reason_slug: str | None
    decided_by_admin_id: AdminId | None
    submitted_at: datetime
    decided_at: datetime | None
