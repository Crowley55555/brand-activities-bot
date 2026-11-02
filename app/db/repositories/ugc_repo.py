from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.errors import ConflictError
from app.core.types.dto import UgcSubmissionRef
from app.core.types.enums import UgcStatus
from app.core.types.ids import AdminId, AttemptId, UgcSubmissionId

_COLUMNS = (
    "id, attempt_id, status, post_url, subscription_claimed_url, file_bucket, file_key, "
    "file_content_type, file_size_bytes, rejection_reason_slug, decided_by_admin_id, "
    "submitted_at, decided_at"
)


_ACTIVE_SUBMISSION_INDEX = "ugc_submission_active"


def _is_active_submission_conflict(exc: IntegrityError) -> bool:
    """Нарушен частичный unique-индекс «одна активная подача на прохождение» (F1)."""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    if getattr(diag, "constraint_name", None) == _ACTIVE_SUBMISSION_INDEX:
        return True
    return _ACTIVE_SUBMISSION_INDEX in str(exc)


def _to_ref(row: Any) -> UgcSubmissionRef:
    return UgcSubmissionRef(
        id=UgcSubmissionId(row.id),
        attempt_id=AttemptId(row.attempt_id),
        status=cast(UgcStatus, row.status),
        post_url=row.post_url,
        subscription_claimed_url=row.subscription_claimed_url,
        file_bucket=row.file_bucket,
        file_key=row.file_key,
        file_content_type=row.file_content_type,
        file_size_bytes=row.file_size_bytes,
        rejection_reason_slug=row.rejection_reason_slug,
        decided_by_admin_id=AdminId(row.decided_by_admin_id)
        if row.decided_by_admin_id is not None
        else None,
        submitted_at=row.submitted_at,
        decided_at=row.decided_at,
    )


class SqlUgcRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _one(self, sql: str, params: dict[str, Any]) -> UgcSubmissionRef | None:
        row = (await self._session.execute(text(sql), params)).one_or_none()
        return _to_ref(row) if row is not None else None

    async def insert_pending_link(
        self, attempt_id: AttemptId, post_url: str, submitted_at: datetime
    ) -> UgcSubmissionRef:
        """Подарок за пост: строка pending без файла (CHECK ugc_file_iff_ingested, миграция
        0013)."""
        try:
            ref = await self._one(
                "INSERT INTO ugc_submissions "
                "(attempt_id, status, post_url, submitted_at, created_at, updated_at) "
                "VALUES (:aid, 'pending', :post, :ts, :ts, :ts) "
                f"RETURNING {_COLUMNS}",
                {"aid": int(attempt_id), "post": post_url, "ts": submitted_at},
            )
        except IntegrityError as exc:
            if _is_active_submission_conflict(exc):
                raise ConflictError("submission_active") from exc
            raise
        if ref is None:  # pragma: no cover - INSERT ... RETURNING всегда отдаёт строку
            raise RuntimeError("ugc_insert_returned_nothing")
        return ref

    async def insert_receiving(
        self,
        attempt_id: AttemptId,
        post_url: str | None,
        subscription_claimed_url: str | None,
        submitted_at: datetime,
    ) -> UgcSubmissionRef:
        try:
            ref = await self._one(
                "INSERT INTO ugc_submissions "
                "(attempt_id, status, post_url, subscription_claimed_url, submitted_at, "
                " created_at, updated_at) "
                "VALUES (:aid, 'receiving', :post, :sub, :ts, :ts, :ts) "
                f"RETURNING {_COLUMNS}",
                {
                    "aid": int(attempt_id),
                    "post": post_url,
                    "sub": subscription_claimed_url,
                    "ts": submitted_at,
                },
            )
        except IntegrityError as exc:
            # Гонка повторов закрыта блокировкой прохождения; индекс 0009 - страховка (F1).
            if _is_active_submission_conflict(exc):
                raise ConflictError("submission_active") from exc
            raise
        if ref is None:  # pragma: no cover - INSERT ... RETURNING всегда отдаёт строку
            raise RuntimeError("ugc_insert_returned_nothing")
        return ref

    async def list_for_attempt(self, attempt_id: AttemptId) -> list[UgcSubmissionRef]:
        rows = (
            await self._session.execute(
                text(f"SELECT {_COLUMNS} FROM ugc_submissions WHERE attempt_id = :aid ORDER BY id"),
                {"aid": int(attempt_id)},
            )
        ).all()
        return [_to_ref(row) for row in rows]

    async def list_files_for_user(
        self, user_id: int
    ) -> list[tuple[int, str, str]]:
        """Фаза 10b-3, I-2 (/forget каскад): (submission_id, bucket, key) по гостю.

        JOIN на `attempts.user_id`: индекс `ugc_submissions.attempt_id` + индекс
        `attempts.user_id` (0001) — быстрый lookup. Строки без файла
        (`file_bucket/file_key IS NULL`, подарок за пост) не возвращаем —
        s3_delete не нужен, но строка всё равно удаляется в `delete_for_user`.
        """
        rows = (
            await self._session.execute(
                text(
                    "SELECT s.id, s.file_bucket, s.file_key "
                    "FROM ugc_submissions s "
                    "JOIN attempts a ON a.id = s.attempt_id "
                    "WHERE a.user_id = :uid "
                    "  AND s.file_bucket IS NOT NULL AND s.file_key IS NOT NULL "
                    "ORDER BY s.id"
                ),
                {"uid": int(user_id)},
            )
        ).all()
        return [
            (int(row.id), str(row.file_bucket), str(row.file_key)) for row in rows
        ]

    async def delete_for_user(self, user_id: int) -> int:
        """Фаза 10b-3, I-2 (/forget): hard delete всех UGC-строк гостя.

        Канон 03 §320: `ugc_submissions.attempt` → `attempts.user_id`. Строки
        прохождений (`attempts`) оставляем — там нет ПДн, финальные статусы и
        журнал пройденных активностей ценны для аналитики.
        Возвращает rowcount.
        """
        result = await self._session.execute(
            text(
                "DELETE FROM ugc_submissions "
                "WHERE attempt_id IN (SELECT id FROM attempts WHERE user_id = :uid)"
            ),
            {"uid": int(user_id)},
        )
        return int(result.rowcount or 0)

    async def get(self, submission_id: UgcSubmissionId) -> UgcSubmissionRef | None:
        return await self._one(
            f"SELECT {_COLUMNS} FROM ugc_submissions WHERE id = :id",
            {"id": int(submission_id)},
        )

    async def set_pending_after_ingest(
        self,
        submission_id: UgcSubmissionId,
        bucket: str,
        key: str,
        content_type: str,
        size_bytes: int,
    ) -> UgcSubmissionRef | None:
        return await self._one(
            "UPDATE ugc_submissions SET status = 'pending', file_bucket = :bucket, "
            "file_key = :key, file_content_type = :ct, file_size_bytes = :size "
            "WHERE id = :id AND status = 'receiving' "
            f"RETURNING {_COLUMNS}",
            {
                "id": int(submission_id),
                "bucket": bucket,
                "key": key,
                "ct": content_type,
                "size": size_bytes,
            },
        )

    async def set_ingest_failed(self, submission_id: UgcSubmissionId) -> UgcSubmissionRef | None:
        return await self._one(
            "UPDATE ugc_submissions SET status = 'ingest_failed' "
            "WHERE id = :id AND status = 'receiving' "
            f"RETURNING {_COLUMNS}",
            {"id": int(submission_id)},
        )

    async def set_approved(
        self, submission_id: UgcSubmissionId, admin_id: AdminId, decided_at: datetime
    ) -> UgcSubmissionRef | None:
        return await self._one(
            "UPDATE ugc_submissions SET status = 'approved', decided_by_admin_id = :admin, "
            "decided_at = :ts WHERE id = :id AND status = 'pending' "
            f"RETURNING {_COLUMNS}",
            {"id": int(submission_id), "admin": int(admin_id), "ts": decided_at},
        )

    async def set_rejected(
        self,
        submission_id: UgcSubmissionId,
        admin_id: AdminId,
        reason_slug: str,
        note: str | None,
        decided_at: datetime,
    ) -> UgcSubmissionRef | None:
        return await self._one(
            "UPDATE ugc_submissions SET status = 'rejected', decided_by_admin_id = :admin, "
            "decided_at = :ts, rejection_reason_slug = :slug, rejection_note = :note "
            "WHERE id = :id AND status = 'pending' "
            f"RETURNING {_COLUMNS}",
            {
                "id": int(submission_id),
                "admin": int(admin_id),
                "ts": decided_at,
                "slug": reason_slug,
                "note": note,
            },
        )
