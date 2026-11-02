"""Возвращаемые значения use-case'ов (08 §2: `core/types/results.py`)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.core.types.dto import ProductRecommendationRef, RewardIssued, SurveyQuestionRef
from app.core.types.enums import ActivityKind, AttemptStatus, UgcStatus
from app.core.types.ids import ActivityId, AttemptId, SurveyQuestionId, UgcSubmissionId


@dataclass(frozen=True)
class StartAttemptResult:
    """Итог старта прохождения. Для опроса `next_question` - первый вопрос."""

    attempt_id: AttemptId
    activity_id: ActivityId
    activity_slug: str
    kind: ActivityKind
    status: AttemptStatus
    attempt_date: date
    next_question: SurveyQuestionRef | None = None
    total_questions: int = 0


@dataclass(frozen=True)
class SubmitAnswerResult:
    attempt_id: AttemptId
    question_id: SurveyQuestionId
    next_question: SurveyQuestionRef | None
    is_last: bool
    answered_count: int
    total_questions: int


@dataclass(frozen=True)
class FinishSurveyResult:
    """Итог опроса: профиль гостя, выданные карточки и награды (одной транзакцией)."""

    attempt_id: AttemptId
    summary: str | None
    profile: Mapping[str, str]
    recommendations: tuple[ProductRecommendationRef, ...]
    rewards: tuple[RewardIssued, ...]
    promocode: str | None


@dataclass(frozen=True)
class UgcStartResult:
    """Старт UGC: прохождение открыто, гостю просят фото (`next_step`)."""

    attempt_id: AttemptId
    activity_slug: str
    next_step: Literal["ask_photo"]


@dataclass(frozen=True)
class UgcAcceptResult:
    """Фото принято в `receiving`; файл докачает ingest-задача (фаза 3c)."""

    submission_id: UgcSubmissionId
    attempt_id: AttemptId
    status: UgcStatus


@dataclass(frozen=True)
class ResubmitResult:
    """Итог повтора после отказа (N4, решение оркестратора).

    `success` - новая строка receiving (`submission_id`). `limit_exceeded` - лимит повторов
    исчерпан, прохождение закрыто rejected (`attempt_closed`). Лимит - результат use-case, не
    исключение: исключение откатило бы закрытие прохождения (UoW откатывает при ошибке).
    """

    kind: Literal["success", "limit_exceeded"]
    submission_id: UgcSubmissionId | None = None
    attempt_closed: bool = False
