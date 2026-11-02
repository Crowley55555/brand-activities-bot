"""Фаза 7d: use-case bulk_create_slots (шаблон «на сегодня») на настоящем Postgres.

NOW = 08:00 МСК 2026-10-12. Диапазоны задаём в aware-UTC (МСК = UTC+3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.common.errors import Forbidden, ValidationError
from app.core.activities.booking import bulk_create_slots
from app.core.types.ids import ActivityId, BrandId
from tests.factories.booking import (
    BRAND_C_SLUG,
    NOW,
    activity_id,
    brand_id,
    clean_booking,
    make_slot,
)


@pytest.fixture(autouse=True)
def _booking_clean(sync_engine: Engine) -> Any:
    clean_booking(sync_engine)
    yield


def _msk(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 12, hour - 3, minute, tzinfo=UTC)


async def _run(
    uow_factory: Any,
    engine: Engine,
    start: datetime,
    end: datetime,
    step: int,
    *,
    brands: frozenset[BrandId] | None = None,
) -> Any:
    allowed = brands if brands is not None else frozenset({BrandId(brand_id(engine, "brand_c"))})
    async with uow_factory() as uow:
        result = await bulk_create_slots(
            uow.repositories,
            ActivityId(activity_id(engine, BRAND_C_SLUG)),
            start,
            end,
            step,
            allowed,
            NOW,
        )
        await uow.commit()
    return result


def _starts(engine: Engine) -> list[datetime]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT start_at FROM slots ORDER BY start_at")).all()
    return [r.start_at for r in rows]


async def test_bulk_creates_expected_slots(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    result = await _run(uow_factory, sync_engine, _msk(10), _msk(12), 30)

    assert (result.created, result.conflicts) == (4, 0)
    assert len(result.created_slot_ids) == 4
    assert _starts(sync_engine) == [_msk(10), _msk(10, 30), _msk(11), _msk(11, 30)]
    with sync_engine.connect() as conn:
        durations = {r[0] for r in conn.execute(text("SELECT duration_minutes FROM slots"))}
    assert durations == {30}


async def test_bulk_skips_conflicts_and_counts_them(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    make_slot(sync_engine, BRAND_C_SLUG, _msk(10))

    result = await _run(uow_factory, sync_engine, _msk(10), _msk(12), 30)

    assert (result.created, result.conflicts) == (3, 1)
    assert len(_starts(sync_engine)) == 4


async def test_bulk_brand_b_creates_nothing(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    await _run(uow_factory, sync_engine, _msk(10), _msk(12), 30)

    again = await _run(uow_factory, sync_engine, _msk(10), _msk(12), 30)

    assert (again.created, again.conflicts) == (0, 4)


@pytest.mark.parametrize("step", [3, 4, 121, 0, -5])
async def test_bulk_step_out_of_range_rejected(
    uow_factory: Any, clean_db: None, sync_engine: Engine, step: int
) -> None:
    with pytest.raises(ValidationError) as exc:
        await _run(uow_factory, sync_engine, _msk(10), _msk(12), step)

    assert exc.value.code == "step_out_of_range"
    assert _starts(sync_engine) == []


async def test_bulk_too_many_slots_rejected(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """09:00..21:00 по 5 минут = 144 слота > 100: ничего не вставлено."""
    with pytest.raises(ValidationError) as exc:
        await _run(uow_factory, sync_engine, _msk(9), _msk(21), 5)

    assert exc.value.code == "too_many_slots"
    assert _starts(sync_engine) == []


async def test_bulk_end_before_start_rejected(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    with pytest.raises(ValidationError) as exc:
        await _run(uow_factory, sync_engine, _msk(12), _msk(10), 30)

    assert exc.value.code == "end_before_start"


async def test_bulk_range_over_24h_rejected(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    with pytest.raises(ValidationError) as exc:
        await _run(uow_factory, sync_engine, _msk(9), datetime(2026, 10, 13, 7, 0, tzinfo=UTC), 120)

    assert exc.value.code == "range_too_large"


async def test_bulk_in_past_rejected_without_partial_insert(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Первый слот 07:00 при NOW 08:00: отказ до вставок, пачка не создаётся частично."""
    with pytest.raises(ValidationError) as exc:
        await _run(uow_factory, sync_engine, _msk(7), _msk(10), 30)

    assert exc.value.code == "slot_in_past"
    assert _starts(sync_engine) == []


async def test_bulk_requires_brand_access(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    with pytest.raises(Forbidden):
        await _run(
            uow_factory,
            sync_engine,
            _msk(10),
            _msk(12),
            30,
            brands=frozenset({BrandId(brand_id(sync_engine, "brand_a"))}),
        )

    assert _starts(sync_engine) == []
