"""Порт Polaroid-приёмки (ADR-40): строки frame_submissions. SQL -
app/db/repositories/frame_repo.py.

Переходы - одиночные UPDATE с предикатом по текущему статусу: повтор или гонка даёт None, а не
дубль.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.activities.photo_frame.types import FrameSubmissionRef
from app.core.types.ids import AttemptId


@runtime_checkable
class FrameRepo(Protocol):
    async def insert_receiving(
        self, attempt_id: AttemptId, created_at: datetime
    ) -> FrameSubmissionRef:
        """Новая строка в статусе receiving: исходника ещё нет (B1)."""
        ...

    async def get(self, submission_id: int) -> FrameSubmissionRef | None: ...

    async def set_source_stored(
        self, submission_id: int, bucket: str, key: str, content_type: str, size_bytes: int
    ) -> FrameSubmissionRef | None:
        """receiving -> queued: исходник лёг в S3. None - строка не в receiving."""
        ...

    async def set_result(
        self,
        submission_id: int,
        bucket: str,
        key: str,
        content_type: str,
        size_bytes: int,
        completed_at: datetime,
    ) -> FrameSubmissionRef | None:
        """queued -> done: результат лёг в S3. None - строка не в queued."""
        ...

    async def set_failed(
        self, submission_id: int, reason: str, completed_at: datetime
    ) -> FrameSubmissionRef | None:
        """queued -> failed: сбой рендера после исходника."""
        ...

    async def set_ingest_failed(
        self, submission_id: int, reason: str, completed_at: datetime
    ) -> FrameSubmissionRef | None:
        """receiving -> ingest_failed: файл не получен из Telegram (исходника нет)."""
        ...

    async def get_result_ref(self, submission_id: int) -> FrameSubmissionRef | None:
        """Строка со статусом done для маршрута /media/frame (только результат)."""
        ...
