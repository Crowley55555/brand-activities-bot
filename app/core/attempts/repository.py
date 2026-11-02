from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from app.core.types.dto import AttemptRef
from app.core.types.enums import AttemptStatus
from app.core.types.ids import ActivityId, AttemptId, ChannelAccountId, UserId


@runtime_checkable
class AttemptRepo(Protocol):
    async def insert_in_progress(
        self,
        user_id: UserId,
        activity_id: ActivityId,
        channel_account_id: ChannelAccountId,
        started_at: datetime,
        attempt_date: date,
        expires_at: datetime,
    ) -> AttemptRef | None:
        """Атомарная вставка in_progress.

        None - у гостя уже есть прохождение этой активности за эту дату
        (partial unique attempts_once_per_day). Защита от гонки - в БД, а не в коде.
        """
        ...

    async def get(self, attempt_id: AttemptId) -> AttemptRef | None: ...

    async def find_today_by_user_activity(
        self, user_id: UserId, activity_id: ActivityId, attempt_date: date
    ) -> AttemptRef | None:
        """Любая строка прохождения гостя по активности за дату, без фильтра по статусу.

        Нужна booking (решение координатора, п.3): отменённая запись тоже занимает день.
        UGC-правила (0008) это не меняют: индекс attempts_once_per_day остаётся прежним.
        """
        ...

    async def close_in_progress(
        self,
        attempt_id: AttemptId,
        status: AttemptStatus,
        summary: str | None,
        finished_at: datetime,
    ) -> AttemptRef | None:
        """UPDATE ... WHERE status='in_progress' RETURNING. None - 0 строк."""
        ...

    async def set_pending_moderation(self, attempt_id: AttemptId) -> AttemptRef | None:
        """UPDATE ... SET status='pending_moderation' WHERE status='in_progress'. None - 0 строк."""
        ...

    async def close_pending_moderation(
        self,
        attempt_id: AttemptId,
        status: AttemptStatus,
        summary: str | None,
        finished_at: datetime,
    ) -> AttemptRef | None:
        """UPDATE ... WHERE status='pending_moderation' RETURNING. None - 0 строк.

        TTL-джоб expire_stale_attempts снимает только in_progress (03 §1), поэтому
        прохождение на модерации им не закрывается.
        """
        ...

    async def cancel_pending_moderation(
        self,
        attempt_id: AttemptId,
        reason: str,
        finished_at: datetime,
    ) -> AttemptRef | None:
        """UPDATE ... SET status='cancelled' WHERE status='pending_moderation'. None - 0 строк."""
        ...

    async def lock_for_update(self, attempt_id: AttemptId) -> None:
        """SELECT ... FOR UPDATE на строке прохождения: повторы встают в очередь (F1)."""
        ...

    async def expire_due(
        self, now: datetime, limit: int = 500
    ) -> list[AttemptId]:
        """C-2 (фаза 10a): атомарный UPDATE просроченных in_progress в expired.

        Один запрос за транзакцию: UPDATE WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED
        LIMIT :limit). Пачка 500 — чтобы один тик не держал базу долго. Возвращает id
        закрытых прохождений для логирования количества. Строки с expires_at IS NULL не
        трогаются (fail-closed без TTL).
        """
        ...

    async def expire_pending_moderation_due(
        self,
        now: datetime,
        activity_ttls: Sequence[tuple[ActivityId, int]],
        limit: int = 500,
    ) -> list[tuple[AttemptId, ActivityId, UserId]]:
        """C-3 (фаза 10a): атомарный UPDATE просроченных pending_moderation в cancelled.

        activity_ttls: (activity_id, ttl_minutes) по тем активностям, у которых есть
        `activities.limits.pending_moderation_ttl_minutes`. Для каждой активности условие:
        status='pending_moderation' AND activity_id=:aid AND updated_at <= now - interval.
        Возвращает (attempt_id, activity_id, user_id) для последующего enqueue
        notify_ingest_failed с system_reason_slug='pending_moderation_expired'.
        Пустой `activity_ttls` -> пустой список: при отсутствии настроенных TTL джоб безопасен.
        Фаза 10b-3, N-1: `limit` параметризован через env EXPIRE_BATCH_SIZE.
        """
        ...
