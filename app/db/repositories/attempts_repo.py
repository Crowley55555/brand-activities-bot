from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types.dto import AttemptRef
from app.core.types.enums import AttemptStatus
from app.core.types.ids import ActivityId, AttemptId, ChannelAccountId, UserId

# slug активности подтягиваем подзапросом, чтобы одинаково работать и в SELECT, и в
# RETURNING (AttemptRef по контракту 08 §4.2 содержит activity_slug).
_COLUMNS = (
    "id, user_id, activity_id, channel_account_id, status, started_at, attempt_date, "
    "finished_at, summary, "
    "(SELECT slug FROM activities WHERE activities.id = attempts.activity_id) "
    "AS activity_slug"
)


def _to_ref(row: Any) -> AttemptRef:
    return AttemptRef(
        id=AttemptId(row.id),
        user_id=UserId(row.user_id),
        activity_id=ActivityId(row.activity_id),
        activity_slug=row.activity_slug,
        channel_account_id=ChannelAccountId(row.channel_account_id),
        status=cast(AttemptStatus, row.status),
        started_at=row.started_at,
        attempt_date=row.attempt_date,
        finished_at=row.finished_at,
        summary=row.summary,
    )


class SqlAttemptRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_in_progress(
        self,
        user_id: UserId,
        activity_id: ActivityId,
        channel_account_id: ChannelAccountId,
        started_at: datetime,
        attempt_date: date,
        expires_at: datetime,
    ) -> AttemptRef | None:
        # ON CONFLICT без цели ловит partial unique attempts_once_per_day. Вторая
        # параллельная транзакция ждёт первую и после её commit получает 0 строк.
        row = (
            await self._session.execute(
                text(
                    "INSERT INTO attempts "
                    "(user_id, activity_id, channel_account_id, status, "
                    " started_at, attempt_date, expires_at, created_at, updated_at) "
                    "VALUES (:uid, :aid, :cid, 'in_progress', :ts, :day, :exp, :ts, :ts) "
                    "ON CONFLICT DO NOTHING "
                    f"RETURNING {_COLUMNS}"
                ),
                {
                    "uid": int(user_id),
                    "aid": int(activity_id),
                    "cid": int(channel_account_id),
                    "ts": started_at,
                    "day": attempt_date,
                    "exp": expires_at,
                },
            )
        ).one_or_none()
        return _to_ref(row) if row is not None else None

    async def set_pending_moderation(self, attempt_id: AttemptId) -> AttemptRef | None:
        row = (
            await self._session.execute(
                text(
                    "UPDATE attempts SET status = 'pending_moderation' "
                    "WHERE id = :id AND status = 'in_progress' "
                    f"RETURNING {_COLUMNS}"
                ),
                {"id": int(attempt_id)},
            )
        ).one_or_none()
        return _to_ref(row) if row is not None else None

    async def close_pending_moderation(
        self,
        attempt_id: AttemptId,
        status: AttemptStatus,
        summary: str | None,
        finished_at: datetime,
    ) -> AttemptRef | None:
        row = (
            await self._session.execute(
                text(
                    "UPDATE attempts SET status = :status, "
                    "finished_at = GREATEST(:ts, started_at), summary = :summary "
                    "WHERE id = :id AND status = 'pending_moderation' "
                    f"RETURNING {_COLUMNS}"
                ),
                {
                    "status": status,
                    "ts": finished_at,
                    "summary": summary,
                    "id": int(attempt_id),
                },
            )
        ).one_or_none()
        return _to_ref(row) if row is not None else None

    async def cancel_pending_moderation(
        self,
        attempt_id: AttemptId,
        reason: str,
        finished_at: datetime,
    ) -> AttemptRef | None:
        row = (
            await self._session.execute(
                text(
                    "UPDATE attempts SET status = 'cancelled', "
                    "finished_at = GREATEST(:ts, started_at), summary = :summary "
                    "WHERE id = :id AND status = 'pending_moderation' "
                    f"RETURNING {_COLUMNS}"
                ),
                {"ts": finished_at, "summary": reason, "id": int(attempt_id)},
            )
        ).one_or_none()
        return _to_ref(row) if row is not None else None

    async def lock_for_update(self, attempt_id: AttemptId) -> None:
        # SELECT ... FOR UPDATE: второй вызов ждёт commit или rollback другой транзакции (F1).
        await self._session.execute(
            text("SELECT id FROM attempts WHERE id = :id FOR UPDATE"),
            {"id": int(attempt_id)},
        )

    async def get(self, attempt_id: AttemptId) -> AttemptRef | None:
        row = (
            await self._session.execute(
                text(f"SELECT {_COLUMNS} FROM attempts WHERE id = :id"),
                {"id": int(attempt_id)},
            )
        ).one_or_none()
        return _to_ref(row) if row is not None else None

    async def find_today_by_user_activity(
        self, user_id: UserId, activity_id: ActivityId, attempt_date: date
    ) -> AttemptRef | None:
        row = (
            await self._session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM attempts "
                    "WHERE user_id = :u AND activity_id = :a AND attempt_date = :d "
                    "ORDER BY id LIMIT 1"
                ),
                {"u": int(user_id), "a": int(activity_id), "d": attempt_date},
            )
        ).one_or_none()
        return _to_ref(row) if row is not None else None

    async def close_in_progress(
        self,
        attempt_id: AttemptId,
        status: AttemptStatus,
        summary: str | None,
        finished_at: datetime,
    ) -> AttemptRef | None:
        # GREATEST: CHECK finished_at >= started_at не должен ронять закрытие из-за
        # небольшого расхождения часов между стартом и финишем.
        row = (
            await self._session.execute(
                text(
                    "UPDATE attempts SET status = :status, "
                    "finished_at = GREATEST(:ts, started_at), summary = :summary "
                    "WHERE id = :id AND status = 'in_progress' "
                    f"RETURNING {_COLUMNS}"
                ),
                {
                    "status": status,
                    "ts": finished_at,
                    "summary": summary,
                    "id": int(attempt_id),
                },
            )
        ).one_or_none()
        return _to_ref(row) if row is not None else None

    async def expire_due(
        self, now: datetime, limit: int = 500
    ) -> list[AttemptId]:
        # C-2 (фаза 10a): скан идёт по индексу attempts_expire_due (0001 §1.3),
        # SKIP LOCKED не ждёт чужих блокировок. Пачка 500 чтобы тик не задерживал базу.
        # expires_at IS NULL оставляем в покое (fail-closed при отсутствии TTL).
        rows = (
            await self._session.execute(
                text(
                    "UPDATE attempts SET status = 'expired', "
                    "finished_at = GREATEST(:ts, started_at), "
                    "summary = COALESCE(summary, 'stale_expired') "
                    "WHERE id IN ( "
                    "  SELECT id FROM attempts "
                    "  WHERE status = 'in_progress' "
                    "    AND expires_at IS NOT NULL AND expires_at <= :ts "
                    "  ORDER BY expires_at "
                    "  LIMIT :lim "
                    "  FOR UPDATE SKIP LOCKED "
                    ") "
                    "RETURNING id"
                ),
                {"ts": now, "lim": int(limit)},
            )
        ).all()
        return [AttemptId(int(row.id)) for row in rows]

    async def expire_pending_moderation_due(
        self,
        now: datetime,
        activity_ttls: Sequence[tuple[ActivityId, int]],
        limit: int = 500,
    ) -> list[tuple[AttemptId, ActivityId, UserId]]:
        # C-3 (фаза 10a): по каждой активности с TTL — свой UPDATE в одной транзакции.
        # Один список активностей (до 10 UGC) + несколько SKIP LOCKED пачек по 500 — не
        # взрыв. Канон 03 §382 (ADR-20): без TTL не трогаем, с TTL закрываем cancelled.
        # Фаза 10b-3, N-1: `limit` параметризован через env EXPIRE_BATCH_SIZE (в scheduler).
        if not activity_ttls:
            return []
        result: list[tuple[AttemptId, ActivityId, UserId]] = []
        for activity_id, ttl_minutes in activity_ttls:
            rows = (
                await self._session.execute(
                    text(
                        "UPDATE attempts SET status = 'cancelled', "
                        "finished_at = GREATEST(:ts, started_at), "
                        "summary = 'pending_moderation_expired' "
                        "WHERE id IN ( "
                        "  SELECT id FROM attempts "
                        "  WHERE status = 'pending_moderation' "
                        "    AND activity_id = :aid "
                        "    AND updated_at <= :ts - make_interval(mins => :mins) "
                        "  ORDER BY updated_at "
                        "  LIMIT :lim "
                        "  FOR UPDATE SKIP LOCKED "
                        ") "
                        "RETURNING id, activity_id, user_id"
                    ),
                    {
                        "ts": now,
                        "aid": int(activity_id),
                        "mins": int(ttl_minutes),
                        "lim": int(limit),
                    },
                )
            ).all()
            result.extend(
                (
                    AttemptId(int(row.id)),
                    ActivityId(int(row.activity_id)),
                    UserId(int(row.user_id)),
                )
                for row in rows
            )
        return result
