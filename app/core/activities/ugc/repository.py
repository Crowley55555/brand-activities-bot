from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.types.dto import UgcSubmissionRef
from app.core.types.ids import AdminId, AttemptId, UgcSubmissionId


@runtime_checkable
class UGCRepo(Protocol):
    async def insert_receiving(
        self,
        attempt_id: AttemptId,
        post_url: str | None,
        subscription_claimed_url: str | None,
        submitted_at: datetime,
    ) -> UgcSubmissionRef:
        """Новая строка ugc_submissions в статусе receiving (файла пока нет)."""
        ...

    async def insert_pending_link(
        self,
        attempt_id: AttemptId,
        post_url: str,
        submitted_at: datetime,
    ) -> UgcSubmissionRef:
        """Строка pending без файла (подарок за пост, ADR-42): file_* NULL, post_url задан."""
        ...

    async def get(self, submission_id: UgcSubmissionId) -> UgcSubmissionRef | None: ...

    async def list_for_attempt(self, attempt_id: AttemptId) -> list[UgcSubmissionRef]:
        """Все строки прохождения по порядку подачи (для лимита повторов)."""
        ...

    async def set_pending_after_ingest(
        self,
        submission_id: UgcSubmissionId,
        bucket: str,
        key: str,
        content_type: str,
        size_bytes: int,
    ) -> UgcSubmissionRef | None:
        """UPDATE ... SET status='pending', file_* WHERE status='receiving'. None - 0 строк."""
        ...

    async def set_ingest_failed(self, submission_id: UgcSubmissionId) -> UgcSubmissionRef | None:
        """UPDATE ... SET status='ingest_failed' WHERE status='receiving'. None - 0 строк."""
        ...

    async def set_approved(
        self, submission_id: UgcSubmissionId, admin_id: AdminId, decided_at: datetime
    ) -> UgcSubmissionRef | None:
        """UPDATE ... SET status='approved' WHERE status='pending'. None - 0 строк."""
        ...

    async def set_rejected(
        self,
        submission_id: UgcSubmissionId,
        admin_id: AdminId,
        reason_slug: str,
        note: str | None,
        decided_at: datetime,
    ) -> UgcSubmissionRef | None:
        """UPDATE ... SET status='rejected' WHERE status='pending'. None - 0 строк."""
        ...
