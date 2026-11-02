"""Чтение файла подачи для раздачи /media (фаза 3b, П2). Только чтение.

Отдаём только подачи, у которых уже есть файл в хранилище (ingest прошёл): у receiving и
ingest_failed файла нет, их адрес NULL и ссылку на них не выдают.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.media.types import SubmissionFileRef
from app.core.types.ids import UgcSubmissionId

_FILE_SQL = (
    "SELECT id, file_bucket, file_key, file_content_type FROM ugc_submissions "
    "WHERE id = :sid AND file_bucket IS NOT NULL AND file_key IS NOT NULL "
    "AND file_content_type IS NOT NULL"
)


class SqlMediaRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_submission_by_id(
        self, submission_id: UgcSubmissionId
    ) -> SubmissionFileRef | None:
        row = (
            await self._session.execute(text(_FILE_SQL), {"sid": int(submission_id)})
        ).one_or_none()
        if row is None:
            return None
        return SubmissionFileRef(
            submission_id=UgcSubmissionId(row.id),
            bucket=str(row.file_bucket),
            key=str(row.file_key),
            content_type=str(row.file_content_type),
        )
