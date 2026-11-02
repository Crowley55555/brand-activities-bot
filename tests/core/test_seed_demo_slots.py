"""Dev-скрипт scripts/seed_demo_slots.py (решение п.6): демо-слоты на день, идемпотентно."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from scripts.seed_demo_slots import seed
from tests.factories.booking import clean_booking

DAY = date(2026, 10, 12)


def _starts(engine: Engine, slug: str) -> list[datetime]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT s.start_at FROM slots s JOIN activities a ON a.id = s.activity_id "
                "WHERE a.slug = :s ORDER BY s.start_at"
            ),
            {"s": slug},
        ).all()
    return [r.start_at for r in rows]


def test_seed_demo_slots_is_idempotent(sync_engine: Engine, clean_db: Any) -> None:
    clean_booking(sync_engine)

    first = seed(sync_engine, DAY)
    second = seed(sync_engine, DAY)

    assert first == {"brand_a_diagnostics_booking": 10, "brand_c_stylist_booking": 8}
    assert second == {"brand_a_diagnostics_booking": 0, "brand_c_stylist_booking": 0}
    brand_a = _starts(sync_engine, "brand_a_diagnostics_booking")
    brand_c = _starts(sync_engine, "brand_c_stylist_booking")
    # 12:00 МСК = 09:00 UTC; 13:00 МСК = 10:00 UTC; последний слот Бренд A - 14:15 МСК
    assert brand_a[0] == datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    assert brand_a[-1] == datetime(2026, 10, 12, 11, 15, tzinfo=UTC)
    assert brand_c[0] == datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    assert brand_c[-1] == datetime(2026, 10, 12, 11, 45, tzinfo=UTC)
    clean_booking(sync_engine)
