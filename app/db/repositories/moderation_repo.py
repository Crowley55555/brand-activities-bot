"""Чтение очереди модерации UGC для админки (ADR-24, фаза 3b). Только чтение.

Запросы идут через admin_brands: очередь сотрудника - это фото брендов, на которые у него есть
связь admin_brands. Решения пишутся через core.activities.ugc (approve/reject), здесь их нет.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.moderation_queue.types import PendingUGCRow, UGCSubmissionDetail
from app.core.types.enums import UgcStatus
from app.core.types.ids import AdminId, AttemptId, BrandId, UgcSubmissionId

_PENDING_SQL = (
    "SELECT s.id AS submission_id, s.attempt_id, b.id AS brand_id, "
    "b.display_name AS brand_name, a.display_name AS activity_name, "
    "s.file_bucket, s.file_key, s.post_url, s.subscription_claimed_url AS subscription_url, "
    "s.submitted_at "
    "FROM admin_brands ab "
    "JOIN brands b ON b.id = ab.brand_id AND b.deleted_at IS NULL "
    "JOIN activities a ON a.brand_id = b.id AND a.kind IN ('ugc', 'confirmation') "
    "AND a.deleted_at IS NULL "
    "JOIN attempts t ON t.activity_id = a.id "
    "JOIN ugc_submissions s ON s.attempt_id = t.id "
    "WHERE ab.admin_id = :admin AND s.status = 'pending' "
    "ORDER BY s.submitted_at ASC, s.id ASC "
    "LIMIT :limit"
)

_DETAIL_SQL = (
    "SELECT s.id AS submission_id, s.attempt_id, s.status, s.file_bucket, s.file_key, "
    "s.post_url, s.subscription_claimed_url AS subscription_url, s.submitted_at, "
    "b.id AS brand_id, b.display_name AS brand_name, a.display_name AS activity_name, "
    "a.kind AS activity_kind, "
    "a.limits AS limits "
    "FROM ugc_submissions s "
    "JOIN attempts t ON t.id = s.attempt_id "
    "JOIN activities a ON a.id = t.activity_id "
    "JOIN brands b ON b.id = a.brand_id "
    "WHERE s.id = :sid"
)

_ACCESS_SQL = "SELECT 1 FROM admin_brands WHERE admin_id = :admin AND brand_id = :brand"


def _reject_reasons(limits: Mapping[str, Any]) -> dict[str, str]:
    """Причины отказа из activities.limits.reject_reasons (правило 5). Нет или битое - пусто."""
    raw = limits.get("reject_reasons")
    if not isinstance(raw, dict):
        return {}
    return {str(slug): str(label) for slug, label in raw.items()}


class SqlModerationRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_pending_for_admin(self, admin_id: AdminId, limit: int) -> list[PendingUGCRow]:
        rows = (
            await self._session.execute(
                text(_PENDING_SQL), {"admin": int(admin_id), "limit": limit}
            )
        ).all()
        return [
            PendingUGCRow(
                submission_id=UgcSubmissionId(row.submission_id),
                attempt_id=AttemptId(row.attempt_id),
                brand_id=BrandId(row.brand_id),
                brand_name=str(row.brand_name),
                activity_name=str(row.activity_name),
                file_bucket=row.file_bucket,
                file_key=row.file_key,
                post_url=row.post_url,
                subscription_url=row.subscription_url,
                submitted_at=row.submitted_at,
            )
            for row in rows
        ]

    async def get_detail(self, submission_id: UgcSubmissionId) -> UGCSubmissionDetail | None:
        row = (
            await self._session.execute(text(_DETAIL_SQL), {"sid": int(submission_id)})
        ).one_or_none()
        if row is None:
            return None
        limits: dict[str, Any] = dict(row.limits or {})
        return UGCSubmissionDetail(
            submission_id=UgcSubmissionId(row.submission_id),
            attempt_id=AttemptId(row.attempt_id),
            status=cast(UgcStatus, row.status),
            brand_id=BrandId(row.brand_id),
            brand_name=str(row.brand_name),
            activity_name=str(row.activity_name),
            activity_kind=str(row.activity_kind),
            file_bucket=row.file_bucket,
            file_key=row.file_key,
            post_url=row.post_url,
            subscription_url=row.subscription_url,
            submitted_at=row.submitted_at,
            reject_reasons=_reject_reasons(limits),
        )

    async def admin_has_brand(self, admin_id: AdminId, brand_id: BrandId) -> bool:
        found = (
            await self._session.execute(
                text(_ACCESS_SQL), {"admin": int(admin_id), "brand": int(brand_id)}
            )
        ).first()
        return found is not None
