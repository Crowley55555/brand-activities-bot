from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types.dto import (
    ActivityRef,
    BrandRef,
    ProductRecommendationRef,
    SurveyOptionRef,
    SurveyQuestionRef,
)
from app.core.types.enums import ActivityKind, QuestionType
from app.core.types.ids import (
    ActivityId,
    BrandId,
    ProductRecommendationId,
    SurveyQuestionId,
)

_ACTIVITY_COLUMNS = "a.id, a.brand_id, a.slug, a.display_name, a.kind, a.limits"


def _to_activity(row: Any) -> ActivityRef:
    return ActivityRef(
        id=ActivityId(row.id),
        brand_id=BrandId(row.brand_id),
        slug=row.slug,
        display_name=row.display_name,
        kind=cast(ActivityKind, row.kind),
        limits=dict(row.limits or {}),
    )


def _to_option(raw: Any) -> SurveyOptionRef:
    profile = raw.get("profile") or {}
    return SurveyOptionRef(
        key=str(raw["key"]),
        label=str(raw["label"]),
        profile={str(attribute): str(value) for attribute, value in profile.items()},
    )


def _to_question(row: Any) -> SurveyQuestionRef:
    return SurveyQuestionRef(
        id=SurveyQuestionId(row.id),
        activity_id=ActivityId(row.activity_id),
        position=int(row.position),
        text=row.text,
        question_type=cast(QuestionType, row.question_type),
        options=tuple(_to_option(option) for option in (row.options or [])),
    )


def _to_card(row: Any) -> ProductRecommendationRef:
    return ProductRecommendationRef(
        id=ProductRecommendationId(row.id),
        activity_id=ActivityId(row.activity_id),
        condition={str(k): str(v) for k, v in (row.condition or {}).items()},
        product_name=row.product_name,
        caption=row.caption,
        product_url=row.product_url,
        position=int(row.position),
    )


_CARD_COLUMNS = "id, activity_id, condition, product_name, caption, product_url, position"


class SqlCatalogRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active_brands(self) -> list[BrandRef]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT id, slug, display_name, tagline "
                    "FROM brands "
                    "WHERE is_active = TRUE "
                    "AND deleted_at IS NULL "
                    "ORDER BY display_name"
                )
            )
        ).all()
        return [
            BrandRef(
                id=BrandId(row.id),
                slug=row.slug,
                display_name=row.display_name,
                tagline=row.tagline,
            )
            for row in rows
        ]

    async def find_activity_by_slug(self, slug: str) -> ActivityRef | None:
        row = (
            await self._session.execute(
                text(
                    f"SELECT {_ACTIVITY_COLUMNS} FROM activities a "
                    "WHERE a.slug = :slug AND a.is_active = TRUE AND a.deleted_at IS NULL"
                ),
                {"slug": slug},
            )
        ).one_or_none()
        return _to_activity(row) if row is not None else None

    async def find_activity_by_id(self, activity_id: ActivityId) -> ActivityRef | None:
        row = (
            await self._session.execute(
                text(f"SELECT {_ACTIVITY_COLUMNS} FROM activities a WHERE a.id = :id"),
                {"id": int(activity_id)},
            )
        ).one_or_none()
        return _to_activity(row) if row is not None else None

    async def list_activities_by_brand(self, brand_slug: str) -> list[ActivityRef]:
        rows = (
            await self._session.execute(
                text(
                    f"SELECT {_ACTIVITY_COLUMNS} FROM activities a "
                    "JOIN brands b ON b.id = a.brand_id "
                    "WHERE b.slug = :slug "
                    "AND b.is_active = TRUE AND b.deleted_at IS NULL "
                    "AND a.is_active = TRUE AND a.deleted_at IS NULL "
                    "ORDER BY a.id"
                ),
                {"slug": brand_slug},
            )
        ).all()
        return [_to_activity(row) for row in rows]

    async def list_survey_questions(self, activity_id: ActivityId) -> list[SurveyQuestionRef]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT id, activity_id, position, text, question_type, options "
                    "FROM survey_questions "
                    "WHERE activity_id = :aid AND deleted_at IS NULL "
                    "ORDER BY position, id"
                ),
                {"aid": int(activity_id)},
            )
        ).all()
        return [_to_question(row) for row in rows]

    async def list_recommendations(
        self, activity_id: ActivityId
    ) -> list[ProductRecommendationRef]:
        rows = (
            await self._session.execute(
                text(
                    f"SELECT {_CARD_COLUMNS} FROM product_recommendations "
                    "WHERE activity_id = :aid AND is_active = TRUE AND deleted_at IS NULL "
                    "ORDER BY position, id"
                ),
                {"aid": int(activity_id)},
            )
        ).all()
        return [_to_card(row) for row in rows]

    async def list_recommendations_by_ids(
        self, ids: Sequence[int]
    ) -> list[ProductRecommendationRef]:
        # Без фильтра is_active/deleted_at: история гостя показывает и такие (ADR-14).
        rows = (
            await self._session.execute(
                text(
                    f"SELECT {_CARD_COLUMNS} FROM product_recommendations "
                    "WHERE id = ANY(:ids) ORDER BY position, id"
                ),
                {"ids": [int(i) for i in ids]},
            )
        ).all()
        return [_to_card(row) for row in rows]

    async def list_activities_with_pending_ttl(self) -> list[ActivityRef]:
        # C-3 (фаза 10a): limits ? 'pending_moderation_ttl_minutes' — JSONB-оператор `?`.
        # Активностей с TTL в MVP единицы (UGC-брендов 3), фильтр по индексу не нужен.
        rows = (
            await self._session.execute(
                text(
                    f"SELECT {_ACTIVITY_COLUMNS} FROM activities a "
                    "WHERE a.limits ? 'pending_moderation_ttl_minutes' "
                    "  AND a.is_active = TRUE AND a.deleted_at IS NULL "
                    "ORDER BY a.id"
                )
            )
        ).all()
        return [_to_activity(row) for row in rows]
