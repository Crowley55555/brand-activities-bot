from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.moderation_queue.types import PendingUGCRow, UGCSubmissionDetail
from app.core.types.ids import AdminId, BrandId, UgcSubmissionId


@runtime_checkable
class ModerationRepo(Protocol):
    async def list_pending_for_admin(self, admin_id: AdminId, limit: int) -> list[PendingUGCRow]:
        """pending-фото брендов сотрудника (admin_brands), старые первыми. Без чужих брендов."""
        ...

    async def get_detail(self, submission_id: UgcSubmissionId) -> UGCSubmissionDetail | None:
        """Карточка без проверки прав: проверку делает сервис (RBAC по бренду)."""
        ...

    async def admin_has_brand(self, admin_id: AdminId, brand_id: BrandId) -> bool:
        """Есть ли у сотрудника связь admin_brands с брендом."""
        ...
