from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jobs.repository import DeadJobRow
from app.core.jobs.types import JobKind, JobRecord
from app.core.types.ids import JobId

_JOB_COLUMNS = "id, kind, payload, status, attempts, max_attempts, idempotency_key, last_error"
_DEAD_COLUMNS = (
    "id, kind, attempts, max_attempts, last_error, updated_at, idempotency_key"
)


def _to_record(row: Any) -> JobRecord:
    return JobRecord(
        id=JobId(int(row.id)),
        kind=str(row.kind),
        payload=dict(row.payload),
        status=str(row.status),
        attempts=int(row.attempts),
        max_attempts=int(row.max_attempts),
        idempotency_key=row.idempotency_key,
        last_error=row.last_error,
    )


def _to_dead(row: Any) -> DeadJobRow:
    return DeadJobRow(
        id=JobId(int(row.id)),
        kind=str(row.kind),
        attempts=int(row.attempts),
        max_attempts=int(row.max_attempts),
        last_error=row.last_error,
        updated_at=row.updated_at,
        idempotency_key=row.idempotency_key,
    )


class SqlJobRepo:
    """Очередь jobs (канон 04 §3.1): постановка, SKIP LOCKED claim, переходы с fencing."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(
        self,
        kind: JobKind,
        payload: Mapping[str, Any],
        run_after: datetime | None,
        idempotency_key: str | None,
    ) -> JobId | None:
        # ON CONFLICT DO NOTHING ловит единственный уникальный индекс jobs_by_idempotency.
        row = (
            await self._session.execute(
                text(
                    "INSERT INTO jobs (kind, payload, idempotency_key, status, run_after, "
                    " created_at, updated_at) "
                    "VALUES (:kind, CAST(:payload AS jsonb), :idem, 'queued', "
                    " COALESCE(CAST(:run_after AS timestamptz), now()), now(), now()) "
                    "ON CONFLICT DO NOTHING "
                    "RETURNING id"
                ),
                {
                    "kind": kind,
                    "payload": json.dumps(dict(payload), ensure_ascii=False),
                    "idem": idempotency_key,
                    "run_after": run_after,
                },
            )
        ).one_or_none()
        return JobId(int(row.id)) if row is not None else None

    async def id_by_idempotency_key(self, idempotency_key: str) -> JobId | None:
        row = (
            await self._session.execute(
                text("SELECT id FROM jobs WHERE idempotency_key = :idem"),
                {"idem": idempotency_key},
            )
        ).one_or_none()
        return JobId(int(row.id)) if row is not None else None

    async def claim(
        self,
        worker_id: str,
        kinds: Sequence[str],
        limit: int,
        visibility_timeout_s: int,
    ) -> list[JobRecord]:
        # FOR UPDATE SKIP LOCKED: параллельные воркеры не берут одну строку (канон 04 §3.1).
        rows = (
            await self._session.execute(
                text(
                    "WITH candidate AS ( "
                    "  SELECT id FROM jobs "
                    "  WHERE status = 'queued' AND run_after <= now() "
                    "    AND kind = ANY(CAST(:kinds AS text[])) "
                    "  ORDER BY run_after, id "
                    "  LIMIT :limit "
                    "  FOR UPDATE SKIP LOCKED "
                    ") "
                    "UPDATE jobs SET status = 'locked', locked_by = :worker, "
                    "  locked_until = now() + make_interval(secs => :visibility), "
                    "  attempts = attempts + 1 "
                    "WHERE id IN (SELECT id FROM candidate) "
                    f"RETURNING {_JOB_COLUMNS}"
                ),
                {
                    "kinds": list(kinds),
                    "limit": limit,
                    "worker": worker_id,
                    "visibility": float(visibility_timeout_s),
                },
            )
        ).all()
        return [_to_record(row) for row in rows]

    async def get(self, job_id: JobId, *, for_update: bool = False) -> JobRecord | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._session.execute(
                text(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = :id{lock}"),
                {"id": int(job_id)},
            )
        ).one_or_none()
        return _to_record(row) if row is not None else None

    async def mark_done(self, job_id: JobId, worker_id: str, last_error: str | None) -> bool:
        row = (
            await self._session.execute(
                text(
                    "UPDATE jobs SET status = 'done', locked_by = NULL, locked_until = NULL, "
                    "  last_error = :err "
                    "WHERE id = :id AND status = 'locked' AND locked_by = :worker RETURNING id"
                ),
                {"id": int(job_id), "worker": worker_id, "err": last_error},
            )
        ).first()
        return row is not None

    async def mark_queued(
        self, job_id: JobId, worker_id: str, delay_s: float, last_error: str
    ) -> bool:
        row = (
            await self._session.execute(
                text(
                    "UPDATE jobs SET status = 'queued', "
                    "  run_after = now() + make_interval(secs => :delay), "
                    "  locked_by = NULL, locked_until = NULL, last_error = :err "
                    "WHERE id = :id AND status = 'locked' AND locked_by = :worker RETURNING id"
                ),
                {
                    "id": int(job_id),
                    "worker": worker_id,
                    "delay": float(delay_s),
                    "err": last_error,
                },
            )
        ).first()
        return row is not None

    async def mark_dead(self, job_id: JobId, worker_id: str, last_error: str) -> bool:
        row = (
            await self._session.execute(
                text(
                    "UPDATE jobs SET status = 'dead', locked_by = NULL, locked_until = NULL, "
                    "  last_error = :err "
                    "WHERE id = :id AND status = 'locked' AND locked_by = :worker RETURNING id"
                ),
                {"id": int(job_id), "worker": worker_id, "err": last_error},
            )
        ).first()
        return row is not None

    async def unlock_expired(self, now: datetime) -> list[JobRecord]:
        rows = (
            await self._session.execute(
                text(
                    "UPDATE jobs SET "
                    "  status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END, "
                    "  locked_by = NULL, locked_until = NULL, last_error = 'lock_expired' "
                    "WHERE status = 'locked' AND locked_until < :now "
                    f"RETURNING {_JOB_COLUMNS}"
                ),
                {"now": now},
            )
        ).all()
        return [_to_record(row) for row in rows if str(row.status) == "dead"]

    # ----- DLQ (фаза 10b-2, I-1) ---------------------------------------------

    async def list_dead(
        self, kind: str | None, limit: int, offset: int
    ) -> list[DeadJobRow]:
        """Выбираем из partial-индекса `jobs_dead_recent` (updated_at DESC WHERE status='dead')."""
        if kind is None:
            rows = (
                await self._session.execute(
                    text(
                        f"SELECT {_DEAD_COLUMNS} FROM jobs "
                        "WHERE status = 'dead' "
                        "ORDER BY updated_at DESC, id DESC "
                        "LIMIT :lim OFFSET :off"
                    ),
                    {"lim": limit, "off": offset},
                )
            ).all()
        else:
            rows = (
                await self._session.execute(
                    text(
                        f"SELECT {_DEAD_COLUMNS} FROM jobs "
                        "WHERE status = 'dead' AND kind = :kind "
                        "ORDER BY updated_at DESC, id DESC "
                        "LIMIT :lim OFFSET :off"
                    ),
                    {"kind": kind, "lim": limit, "off": offset},
                )
            ).all()
        return [_to_dead(row) for row in rows]

    async def count_dead(self, kind: str | None) -> int:
        if kind is None:
            value = (
                await self._session.execute(
                    text("SELECT count(*) AS n FROM jobs WHERE status = 'dead'")
                )
            ).scalar_one()
        else:
            value = (
                await self._session.execute(
                    text(
                        "SELECT count(*) AS n FROM jobs "
                        "WHERE status = 'dead' AND kind = :kind"
                    ),
                    {"kind": kind},
                )
            ).scalar_one()
        return int(value)

    async def revive_dead(self, job_id: JobId, now: datetime) -> bool:
        """Атомарный UPDATE: dead → queued. attempts=0, locked_*=NULL, last_error=NULL.

        `run_after = :now` ставит задачу в очередь немедленно (клаймер возьмёт её
        на следующем тике). rowcount=0 — задача уже не dead.
        """
        row = (
            await self._session.execute(
                text(
                    "UPDATE jobs SET status = 'queued', attempts = 0, "
                    "  locked_by = NULL, locked_until = NULL, last_error = NULL, "
                    "  run_after = :now "
                    "WHERE id = :id AND status = 'dead' "
                    "RETURNING id"
                ),
                {"id": int(job_id), "now": now},
            )
        ).first()
        return row is not None

    async def bury_dead(self, job_id: JobId, last_error: str) -> bool:
        """Атомарный UPDATE: dead → done, last_error = <маркер>.

        Хранить строку (а не удалять) — чтобы в аудите остался след, какой джоб
        был похоронен; done — терминальный статус, повтор не берётся.
        """
        row = (
            await self._session.execute(
                text(
                    "UPDATE jobs SET status = 'done', last_error = :err "
                    "WHERE id = :id AND status = 'dead' "
                    "RETURNING id"
                ),
                {"id": int(job_id), "err": last_error},
            )
        ).first()
        return row is not None
