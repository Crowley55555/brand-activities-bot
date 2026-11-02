from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.core.types.dto import (
    ActivityRef,
    BrandRef,
    ProductRecommendationRef,
    SurveyQuestionRef,
)
from app.core.types.ids import ActivityId


@runtime_checkable
class CatalogRepo(Protocol):
    """Чтение справочников. Единый порт: это только SELECT'ы (08 §2)."""

    async def list_active_brands(self) -> list[BrandRef]: ...

    async def find_activity_by_slug(self, slug: str) -> ActivityRef | None:
        """Только активная и не удалённая активность (то, что можно запустить)."""
        ...

    async def find_activity_by_id(self, activity_id: ActivityId) -> ActivityRef | None:
        """Любая существующая активность: идущее прохождение можно завершить."""
        ...

    async def list_activities_by_brand(self, brand_slug: str) -> list[ActivityRef]:
        """Активные не удалённые активности активного бренда, по id."""
        ...

    async def list_survey_questions(self, activity_id: ActivityId) -> list[SurveyQuestionRef]:
        """Не удалённые вопросы активности по `position`."""
        ...

    async def list_recommendations(self, activity_id: ActivityId) -> list[ProductRecommendationRef]:
        """Активные не удалённые карточки активности по `position`."""
        ...

    async def list_recommendations_by_ids(
        self, ids: Sequence[int]
    ) -> list[ProductRecommendationRef]:
        """Карточки по id, включая деактивированные и soft-deleted (история гостя, ADR-14)."""
        ...

    async def list_activities_with_pending_ttl(self) -> list[ActivityRef]:
        """Активности, у которых в `limits` задан ключ `pending_moderation_ttl_minutes`.

        Нужен джобу `expire_pending_moderation` (фаза 10a): он читает TTL из каждой
        активности и переводит просроченные `pending_moderation` в `cancelled`.
        Фильтр `is_active = TRUE AND deleted_at IS NULL` оставлен: выключенные
        активности джоб не трогает.
        """
        ...
