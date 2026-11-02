"""core.jobs.enqueue (ADR-18, фаза 3a): идемпотентность по ключу и проверка kind.

Воркер, claim_batch и шедулер - фаза 3c; здесь только постановка задачи в таблицу jobs.
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine

from app.common.errors import ValidationError
from app.core.jobs import enqueue
from tests.factories.survey import UowFactory
from tests.factories.ugc import count_rows


async def test_enqueue_same_key_returns_same_job(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    async with uow_factory() as uow:
        first = await enqueue(
            uow.repositories.jobs,
            "ingest_ugc_file",
            {"submission_id": 1, "tg_file_id": "x"},
            idempotency_key="ugc_ingest:1",
        )
    async with uow_factory() as uow:
        second = await enqueue(
            uow.repositories.jobs,
            "ingest_ugc_file",
            {"submission_id": 1, "tg_file_id": "x"},
            idempotency_key="ugc_ingest:1",
        )

    assert first == second
    assert count_rows(sync_engine, "jobs") == 1


async def test_enqueue_rejects_unknown_kind(uow_factory: UowFactory, clean_db: None) -> None:
    with pytest.raises(ValidationError) as exc:
        async with uow_factory() as uow:
            await enqueue(uow.repositories.jobs, "no_such_kind", {})  # type: ignore[arg-type]

    assert exc.value.code == "job_kind_unknown"
