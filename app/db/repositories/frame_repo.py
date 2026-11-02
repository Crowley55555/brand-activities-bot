"""SQL-реализация порта Polaroid (ADR-40): frame_submissions.

Переходы статуса - одиночные UPDATE с предикатом по текущему статусу и RETURNING: повтор или гонка
даёт None, а не дубль (канон 04 §5.1). CHECK-ограничения миграции 0013 держат согласованность полей.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.activities.photo_frame.types import FrameSubmissionRef
from app.core.media.frames import FrameResultRef
from app.core.types.ids import AttemptId

_COLUMNS = (
    "id, attempt_id, status, source_bucket, source_key, source_content_type, "
    "source_size_bytes, result_bucket, result_key, result_content_type, result_size_bytes, "
    "failure_reason"
)


def _to_ref(row: Any) -> FrameSubmissionRef:
    return FrameSubmissionRef(
        id=int(row.id),
        attempt_id=AttemptId(int(row.attempt_id)),
        status=str(row.status),
        source_bucket=row.source_bucket,
        source_key=row.source_key,
        source_content_type=row.source_content_type,
        source_size_bytes=None if row.source_size_bytes is None else int(row.source_size_bytes),
        result_bucket=row.result_bucket,
        result_key=row.result_key,
        result_content_type=row.result_content_type,
        result_size_bytes=None if row.result_size_bytes is None else int(row.result_size_bytes),
        failure_reason=row.failure_reason,
    )


class SqlFrameRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _one(self, sql: str, params: dict[str, Any]) -> FrameSubmissionRef | None:
        row = (await self._session.execute(text(sql), params)).one_or_none()
        return None if row is None else _to_ref(row)

    async def insert_receiving(
        self, attempt_id: AttemptId, created_at: datetime
    ) -> FrameSubmissionRef:
        ref = await self._one(
            "INSERT INTO frame_submissions (attempt_id, status, created_at, updated_at) "
            f"VALUES (:aid, 'receiving', :ts, :ts) RETURNING {_COLUMNS}",
            {"aid": int(attempt_id), "ts": created_at},
        )
        if ref is None:  # pragma: no cover - INSERT ... RETURNING всегда отдаёт строку
            raise RuntimeError("frame_insert_returned_nothing")
        return ref

    async def get(self, submission_id: int) -> FrameSubmissionRef | None:
        return await self._one(
            f"SELECT {_COLUMNS} FROM frame_submissions WHERE id = :id", {"id": submission_id}
        )

    async def set_source_stored(
        self, submission_id: int, bucket: str, key: str, content_type: str, size_bytes: int
    ) -> FrameSubmissionRef | None:
        return await self._one(
            "UPDATE frame_submissions SET status = 'queued', source_bucket = :b, "
            "source_key = :k, source_content_type = :ct, source_size_bytes = :sz, "
            "updated_at = now() WHERE id = :id AND status = 'receiving' "
            f"RETURNING {_COLUMNS}",
            {"id": submission_id, "b": bucket, "k": key, "ct": content_type, "sz": size_bytes},
        )

    async def set_result(
        self,
        submission_id: int,
        bucket: str,
        key: str,
        content_type: str,
        size_bytes: int,
        completed_at: datetime,
    ) -> FrameSubmissionRef | None:
        return await self._one(
            "UPDATE frame_submissions SET status = 'done', result_bucket = :b, "
            "result_key = :k, result_content_type = :ct, result_size_bytes = :sz, "
            "completed_at = :ts, updated_at = now() WHERE id = :id AND status = 'queued' "
            f"RETURNING {_COLUMNS}",
            {
                "id": submission_id,
                "b": bucket,
                "k": key,
                "ct": content_type,
                "sz": size_bytes,
                "ts": completed_at,
            },
        )

    async def set_failed(
        self, submission_id: int, reason: str, completed_at: datetime
    ) -> FrameSubmissionRef | None:
        return await self._one(
            "UPDATE frame_submissions SET status = 'failed', failure_reason = :reason, "
            "completed_at = :ts, updated_at = now() WHERE id = :id AND status = 'queued' "
            f"RETURNING {_COLUMNS}",
            {"id": submission_id, "reason": reason, "ts": completed_at},
        )

    async def set_ingest_failed(
        self, submission_id: int, reason: str, completed_at: datetime
    ) -> FrameSubmissionRef | None:
        return await self._one(
            "UPDATE frame_submissions SET status = 'ingest_failed', failure_reason = :reason, "
            "completed_at = :ts, updated_at = now() WHERE id = :id AND status = 'receiving' "
            f"RETURNING {_COLUMNS}",
            {"id": submission_id, "reason": reason, "ts": completed_at},
        )

    async def get_result_ref(self, frame_submission_id: int) -> FrameResultRef | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, result_bucket, result_key, result_content_type "
                    "FROM frame_submissions WHERE id = :id AND status = 'done'"
                ),
                {"id": frame_submission_id},
            )
        ).one_or_none()
        if row is None or row.result_bucket is None or row.result_key is None:
            return None
        return FrameResultRef(
            frame_submission_id=int(row.id),
            bucket=str(row.result_bucket),
            key=str(row.result_key),
            content_type=str(row.result_content_type or "image/jpeg"),
        )
