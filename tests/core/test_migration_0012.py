"""Миграция 0012 (ADR-35, решение п.6): две активности booking, без слотов.

T15 - по промту, с уточнением координатора: проверяем только две активности и их limits.
Слоты в миграцию не входят (демо-слоты - scripts/seed_demo_slots.py). База тестов общая для
сессии, поэтому перед downgrade удаляем записи, которые ссылаются на booking-активности
(FK RESTRICT); на проде downgrade 0012 не запускаем.
"""

from __future__ import annotations

from typing import Any

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from alembic import command

SLUGS = {"brand_a_diagnostics_booking", "brand_c_stylist_booking"}
PREVIOUS = "0011_seed_brand_b_system_reasons"


def _booking_rows(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.slug, a.kind, a.limits, b.slug AS brand_slug "
                "FROM activities a JOIN brands b ON b.id = a.brand_id "
                "WHERE a.slug IN ('brand_a_diagnostics_booking', 'brand_c_stylist_booking')"
            )
        ).all()
    return {str(r.slug): r for r in rows}


def _clear_booking_dependents(engine: Engine) -> None:
    """Порядок важен: слоты (с каскадом на appointments), потом награды, потом попытки."""
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE slots RESTART IDENTITY CASCADE"))
        conn.execute(
            text(
                "DELETE FROM rewards WHERE attempt_id IN ("
                "  SELECT t.id FROM attempts t JOIN activities a ON a.id = t.activity_id"
                "  WHERE a.slug IN ('brand_a_diagnostics_booking', 'brand_c_stylist_booking'))"
            )
        )
        conn.execute(
            text(
                "DELETE FROM attempts WHERE activity_id IN ("
                "  SELECT id FROM activities"
                "  WHERE slug IN ('brand_a_diagnostics_booking', 'brand_c_stylist_booking'))"
            )
        )


def test_migration_0012_up_down_up(pg_url: str, sync_engine: Engine, clean_db: None) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)

    rows = _booking_rows(sync_engine)
    assert set(rows) == SLUGS
    assert all(r.kind == "booking" for r in rows.values())
    assert rows["brand_a_diagnostics_booking"].brand_slug == "brand_a"
    assert rows["brand_c_stylist_booking"].brand_slug == "brand_c"

    brand_a = rows["brand_a_diagnostics_booking"].limits
    assert brand_a["has_reminders"] is False
    assert "leaflet_url" not in brand_a

    brand_c = rows["brand_c_stylist_booking"].limits
    assert brand_c["has_reminders"] is True
    assert brand_c["remind_before_minutes"] == 5
    assert brand_c["auto_cancel_after_minutes"] == 5
    assert brand_c["has_leaflet"] is True
    assert brand_c["leaflet_url"] == "/static/leaflets/brand_c.pdf"
    assert brand_c["leaflet_title"] == "Листовка Бренд C"

    _clear_booking_dependents(sync_engine)
    command.downgrade(cfg, PREVIOUS)
    assert _booking_rows(sync_engine) == {}

    command.upgrade(cfg, "head")
    assert set(_booking_rows(sync_engine)) == SLUGS
    # Демо-слоты не входят в миграцию (решение п.6): после чистого upgrade слотов нет.
    with sync_engine.connect() as conn:
        slot_count = conn.execute(
            text(
                "SELECT count(*) FROM slots s JOIN activities a ON a.id = s.activity_id "
                "WHERE a.kind = 'booking'"
            )
        ).scalar_one()
    assert slot_count == 0
