"""Фаза 10b-2 — core операции DLQ (I-1): list_dead / revive_dead / bury_dead.

Правило 3 (бизнес-логика в core): фильтр по kind, revive с reset attempts, bury с
whitelist слагов, запись в audit_log. Правило 2: в details audit — только слаг
причины захоронения (без свободного текста, без ПДн).

ManualClock session-wide фиксирован на 2026-10-12 05:00 UTC (tests/conftest.py);
jobs.updated_at ставит триггер (`now()` в БД, не ManualClock) — порядок сортировки
проверяем по id/приоритету updated_at после `_bump`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.common.errors import ConflictError, ValidationError
from app.core.jobs.dlq import (
    BURY_REASON_SLUGS,
    bury_dead,
    list_dead,
    revive_dead,
)
from app.core.types.ids import AdminId, JobId
from tests.factories.booking import audit_rows
from tests.factories.survey import UowFactory
from tests.factories.ugc import create_admin

NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)


# ---------------------------------------------------------------- хелперы фикстур


def _insert_dead(
    engine: Engine,
    *,
    kind: str = "notify_reward",
    payload: dict[str, Any] | None = None,
    last_error: str = "boom",
    attempts: int = 6,
    idempotency_key: str | None = None,
) -> int:
    """Положить в таблицу dead-задачу напрямую (минуя claim/mark_dead)."""
    import json

    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO jobs (kind, payload, idempotency_key, status, attempts, "
                "  run_after, last_error, created_at, updated_at) "
                "VALUES (:kind, CAST(:payload AS jsonb), :idem, 'dead', :attempts, "
                "  now(), :err, now(), now()) "
                "RETURNING id"
            ),
            {
                "kind": kind,
                "payload": json.dumps(payload or {}),
                "idem": idempotency_key,
                "attempts": attempts,
                "err": last_error,
            },
        ).one()
    return int(row.id)


def _insert_queued(engine: Engine, *, kind: str = "notify_reward") -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO jobs (kind, payload, status, attempts, run_after, "
                "  created_at, updated_at) "
                "VALUES (:kind, CAST('{}' AS jsonb), 'queued', 0, now(), now(), now()) "
                "RETURNING id"
            ),
            {"kind": kind},
        ).one()
    return int(row.id)


def _state(engine: Engine, job_id: int) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT status, attempts, last_error, locked_by, locked_until "
                "FROM jobs WHERE id = :id"
            ),
            {"id": job_id},
        ).one()


# ---------------------------------------------------------------- 1. list_dead фильтр по kind


async def test_list_dead_filters_by_kind(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    """Две dead-задачи разных kind: фильтр по kind возвращает одну."""
    jid_notify = _insert_dead(sync_engine, kind="notify_reward")
    _insert_dead(sync_engine, kind="telegram_send")

    async with uow_factory() as uow:
        rows, total = await list_dead(
            uow.repositories.jobs, kind="notify_reward", limit=50, offset=0
        )

    assert total == 1
    assert len(rows) == 1
    assert int(rows[0].id) == jid_notify
    assert rows[0].kind == "notify_reward"


# ---------------------------------------------------------------- 2. paginate newest first


async def test_list_dead_paginates_newest_first(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    """>50 задач → первая страница 50 новейших по updated_at (индекс jobs_dead_recent)."""
    inserted: list[int] = []
    for _ in range(55):
        inserted.append(_insert_dead(sync_engine, kind="notify_reward"))
        time.sleep(0.001)

    async with uow_factory() as uow:
        rows, total = await list_dead(
            uow.repositories.jobs, kind=None, limit=50, offset=0
        )

    assert total == 55
    assert len(rows) == 50
    # Новейшие сверху: индекс jobs_dead_recent сортирует по updated_at DESC.
    # Все 55 вставлены за секунды — updated_at у некоторых совпадает, но
    # id уникальны. Проверяем, что самая первая вставленная (минимальный id)
    # НЕ попала в первую страницу.
    returned_ids = {int(r.id) for r in rows}
    assert inserted[0] not in returned_ids
    # А последние 5 — все там.
    assert set(inserted[-5:]) <= returned_ids


# ---------------------------------------------------------------- 3. revive → queued, сброс попыток


async def test_revive_dead_moves_to_queued_and_resets_attempts(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    admin_id = create_admin(sync_engine)
    job_id = _insert_dead(
        sync_engine, kind="notify_reward", attempts=6, last_error="boom"
    )

    async with uow_factory() as uow:
        await revive_dead(
            uow.repositories.jobs,
            uow.repositories.audit,
            AdminId(admin_id),
            JobId(job_id),
            NOW,
        )

    row = _state(sync_engine, job_id)
    assert row.status == "queued"
    assert row.attempts == 0
    assert row.locked_by is None
    assert row.locked_until is None
    assert row.last_error is None


# ---------------------------------------------------------------- 4. revive не-dead → ConflictError


async def test_revive_already_queued_conflict(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    """Задача не dead → ConflictError('not_dead') (кто-то другой уже чинил)."""
    admin_id = create_admin(sync_engine)
    job_id = _insert_queued(sync_engine)

    with pytest.raises(ConflictError) as exc:
        async with uow_factory() as uow:
            await revive_dead(
                uow.repositories.jobs,
                uow.repositories.audit,
                AdminId(admin_id),
                JobId(job_id),
                NOW,
            )
    assert exc.value.code == "not_dead"
    # Статус не меняется, аудит не пишется.
    assert _state(sync_engine, job_id).status == "queued"
    assert audit_rows(sync_engine, "jobs_dead_revive") == []


# ---------------------------------------------------------------- 5. bury с валидным слагом


async def test_bury_dead_marks_done_with_reason_slug(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    admin_id = create_admin(sync_engine)
    job_id = _insert_dead(sync_engine, kind="notify_reward")

    async with uow_factory() as uow:
        await bury_dead(
            uow.repositories.jobs,
            uow.repositories.audit,
            AdminId(admin_id),
            JobId(job_id),
            "manually_delivered",
            NOW,
        )

    row = _state(sync_engine, job_id)
    assert row.status == "done"
    assert row.last_error == "buried:manually_delivered"


# ---------------------------------- 6. bury с unknown-слагом → ValidationError


async def test_bury_dead_unknown_reason_rejected(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    admin_id = create_admin(sync_engine)
    job_id = _insert_dead(sync_engine, kind="notify_reward")

    with pytest.raises(ValidationError) as exc:
        async with uow_factory() as uow:
            await bury_dead(
                uow.repositories.jobs,
                uow.repositories.audit,
                AdminId(admin_id),
                JobId(job_id),
                "arbitrary_text",
                NOW,
            )
    assert exc.value.code == "bury_reason_unknown"
    # Задача не трогается, аудит не пишется.
    assert _state(sync_engine, job_id).status == "dead"
    assert audit_rows(sync_engine, "jobs_dead_bury") == []


# ---------------------------------------------------------------- 7. revive пишет audit


async def test_revive_writes_audit(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    admin_id = create_admin(sync_engine)
    job_id = _insert_dead(sync_engine, kind="notify_reward")

    async with uow_factory() as uow:
        await revive_dead(
            uow.repositories.jobs,
            uow.repositories.audit,
            AdminId(admin_id),
            JobId(job_id),
            NOW,
        )

    logged = audit_rows(sync_engine, "jobs_dead_revive")
    assert len(logged) == 1
    assert logged[0].actor_admin_id == admin_id
    assert logged[0].object_type == "jobs"
    assert logged[0].object_id == job_id


# ---------------------------------- 8. bury пишет audit, details — слаг


async def test_bury_writes_audit(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    admin_id = create_admin(sync_engine)
    job_id = _insert_dead(sync_engine, kind="notify_reward")

    async with uow_factory() as uow:
        await bury_dead(
            uow.repositories.jobs,
            uow.repositories.audit,
            AdminId(admin_id),
            JobId(job_id),
            "data_invalid",
            NOW,
        )

    logged = audit_rows(sync_engine, "jobs_dead_bury")
    assert len(logged) == 1
    assert logged[0].actor_admin_id == admin_id
    assert logged[0].object_type == "jobs"
    assert logged[0].object_id == job_id
    with sync_engine.connect() as conn:
        details = conn.execute(
            text(
                "SELECT details FROM audit_log WHERE action = 'jobs_dead_bury' "
                "ORDER BY id DESC LIMIT 1"
            )
        ).scalar_one()
    assert details == {"reason": "data_invalid"}


# ---------------------------------------------------------------- доп. контракт BURY_REASON_SLUGS


def test_bury_reason_slugs_set() -> None:
    """Whitelist слагов — заморожен и содержит минимум три опции (промт §3.5)."""
    assert "manually_delivered" in BURY_REASON_SLUGS
    assert "data_invalid" in BURY_REASON_SLUGS
    assert "operator_notified" in BURY_REASON_SLUGS
    assert isinstance(BURY_REASON_SLUGS, frozenset)
