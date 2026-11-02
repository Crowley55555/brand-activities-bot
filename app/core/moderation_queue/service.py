"""Очередь и карточка UGC для сотрудника бренда (ADR-24, фаза 3b).

Права - по admin_brands: сотрудник видит и решает только фото своих брендов. Проверка идёт и на
чтении, и перед решением (POST approve/reject): иначе чужой бренд решался бы напрямую.
"""

from __future__ import annotations

from typing import Final

from app.common.errors import Forbidden, NotFound
from app.core.moderation_queue.repository import ModerationRepo
from app.core.moderation_queue.types import PendingUGCRow, UGCSubmissionDetail
from app.core.types.ids import AdminId, UgcSubmissionId

DEFAULT_QUEUE_LIMIT: Final[int] = 50
MAX_QUEUE_LIMIT: Final[int] = 200


async def list_pending_for_brand(
    repo: ModerationRepo, admin_id: AdminId, limit: int = DEFAULT_QUEUE_LIMIT
) -> list[PendingUGCRow]:
    """Очередь pending по всем брендам сотрудника. Лимит ограничен, чтобы страница не росла."""
    bounded = min(max(limit, 1), MAX_QUEUE_LIMIT)
    return await repo.list_pending_for_admin(admin_id, bounded)


async def get_submission_detail(
    repo: ModerationRepo, submission_id: UgcSubmissionId, admin_id: AdminId
) -> UGCSubmissionDetail:
    """Карточка фото. Нет такого фото - NotFound; бренд не у сотрудника - Forbidden (T5, E1)."""
    detail = await repo.get_detail(submission_id)
    if detail is None:
        raise NotFound("ugc_not_found")
    if not await repo.admin_has_brand(admin_id, detail.brand_id):
        raise Forbidden("brand_access_denied")
    return detail
