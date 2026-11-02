"""Строки очереди и карточка решения модератора (фаза 3b, ADR-24).

ПДн сюда не попадают: только id, название бренда и активности, ссылки гостя на пост и профиль и
ключ файла в хранилище. Имя, телефон и почту модератору экран не показывает.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from app.core.types.enums import UgcStatus
from app.core.types.ids import AttemptId, BrandId, UgcSubmissionId


@dataclass(frozen=True)
class PendingUGCRow:
    """Строка очереди: фото в статусе pending бренда, на который у сотрудника есть доступ."""

    submission_id: UgcSubmissionId
    attempt_id: AttemptId
    brand_id: BrandId
    brand_name: str
    activity_name: str
    # Подарок за пост (фаза 5) - без файла: file_* NULL, фото в очереди не показываем.
    file_bucket: str | None
    file_key: str | None
    post_url: str | None
    subscription_url: str | None
    submitted_at: datetime


@dataclass(frozen=True)
class UGCSubmissionDetail:
    """Карточка для решения. Причины отказа - из activities.limits.reject_reasons (правило 5).

    activity_kind ('ugc' | 'confirmation') выбирает, какое ядро решает подачу (B-3, админка).
    """

    submission_id: UgcSubmissionId
    attempt_id: AttemptId
    status: UgcStatus
    brand_id: BrandId
    brand_name: str
    activity_name: str
    activity_kind: str
    file_bucket: str | None
    file_key: str | None
    post_url: str | None
    subscription_url: str | None
    submitted_at: datetime
    reject_reasons: Mapping[str, str]
