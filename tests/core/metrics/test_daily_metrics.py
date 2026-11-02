"""Фаза 7c - use-case daily_metrics."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.engine import Engine

from app.common.errors import ValidationError
from app.core.metrics import daily_metrics
from app.db.unit_of_work import UnitOfWork

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


async def test_daily_metrics_returns_days_rows(
    clean_db: None, sync_engine: Engine, uow_factory: Callable[[], UnitOfWork]
) -> None:
    async with uow_factory() as uow:
        rows = await daily_metrics(uow.repositories.metrics, 7, None, NOW)

    assert len(rows) == 7
    assert rows[0].day == date(2026, 10, 14)  # новые сверху
    assert rows[-1].day == date(2026, 10, 8)


async def test_daily_metrics_fills_zero_for_empty_days(
    clean_db: None, sync_engine: Engine, uow_factory: Callable[[], UnitOfWork]
) -> None:
    async with uow_factory() as uow:
        rows = await daily_metrics(uow.repositories.metrics, 3, "brand_a", NOW)

    assert all(
        (r.registrations, r.completed_attempts, r.promocodes_issued, r.vouchers_issued)
        == (0, 0, 0, 0)
        for r in rows
    )


@pytest.mark.parametrize("days", [0, 91, -1])
async def test_daily_metrics_validation_days(
    days: int, uow_factory: Callable[[], UnitOfWork]
) -> None:
    async with uow_factory() as uow:
        with pytest.raises(ValidationError):
            await daily_metrics(uow.repositories.metrics, days, None, NOW)
