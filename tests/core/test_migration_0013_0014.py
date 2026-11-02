"""Миграции 0013 (схема Бренд D) и 0014 (сид): up/down/up и содержимое (ADR-42, ADR-43, B3).

База тестов общая для сессии, поэтому перед downgrade удаляем записи, которые мешают откату
(FK RESTRICT и CHECK старой схемы). На проде downgrade 0013/0014 не запускаем.
"""

from __future__ import annotations

from typing import Any

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from alembic import command

BRAND_D_SLUGS = {"brand_d_wheel", "brand_d_polaroid", "brand_d_confirmation"}
PREVIOUS = "0012_seed_booking_activities"
PRIZE_TITLES = {
    "Пробник крема (JS Big)",
    "Пробник крема (JS Small)",
    "Комплимент стилиста",
    "Промокод -10%",
    "Промокод -20%",
}


def _brand_d_activities(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.slug, a.kind, a.limits, b.slug AS brand_slug "
                "FROM activities a JOIN brands b ON b.id = a.brand_id "
                "WHERE a.slug IN ('brand_d_wheel', 'brand_d_polaroid', 'brand_d_confirmation')"
            )
        ).all()
    return {str(row.slug): row for row in rows}


def _brand_d_prizes(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT p.title, p.prize_kind, p.weight, p.stock_left, p.code_template "
                    "FROM wheel_prizes p JOIN activities a ON a.id = p.activity_id "
                    "WHERE a.slug = 'brand_d_wheel' ORDER BY p.id"
                )
            ).all()
        )


def _clear_brand_d_dependents(engine: Engine) -> None:
    """Порядок важен: спины колеса и рамки, file-less строки UGC (старый CHECK их не примет).

    Промокод-призы других тестов тоже убираем: downgrade 0013 сносит code_template, а повторный
    upgrade ставит CHECK wheel_prizes_code_template, который такие строки не пройдут.
    """
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE wheel_spins, frame_submissions RESTART IDENTITY"))
        conn.execute(text("DELETE FROM wheel_prizes WHERE prize_kind = 'promocode'"))
        conn.execute(text("DELETE FROM ugc_submissions WHERE file_bucket IS NULL"))
        conn.execute(
            text(
                "DELETE FROM rewards WHERE attempt_id IN ("
                "  SELECT t.id FROM attempts t JOIN activities a ON a.id = t.activity_id"
                "  WHERE a.slug IN ('brand_d_wheel', 'brand_d_polaroid', 'brand_d_confirmation'))"
            )
        )
        conn.execute(
            text(
                "DELETE FROM attempts WHERE activity_id IN ("
                "  SELECT id FROM activities"
                "  WHERE slug IN ('brand_d_wheel', 'brand_d_polaroid', 'brand_d_confirmation'))"
            )
        )


def test_migration_0013_0014_up_down_up(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)

    activities = _brand_d_activities(sync_engine)
    assert set(activities) == BRAND_D_SLUGS
    assert activities["brand_d_wheel"].kind == "wheel"
    assert activities["brand_d_polaroid"].kind == "photo_frame"
    assert activities["brand_d_confirmation"].kind == "confirmation"
    assert all(row.brand_slug == "brand_d" for row in activities.values())
    assert activities["brand_d_polaroid"].limits["system_reject_reasons"] == {
        "polaroid_render_failed": "Не удалось обработать фото"
    }
    assert activities["brand_d_confirmation"].limits["promocode_template"] == "BRAND_D-{random8}"

    prizes = _brand_d_prizes(sync_engine)
    assert {row.title for row in prizes} == PRIZE_TITLES
    promo = [row for row in prizes if row.prize_kind == "promocode"]
    assert len(promo) == 2
    assert all(row.code_template and row.code_template.startswith("BRAND_D") for row in promo)
    assert [row.code_template for row in prizes if row.prize_kind != "promocode"] == [
        None,
        None,
        None,
    ]
    stock_by_title = {row.title: row.stock_left for row in prizes}
    assert stock_by_title["Пробник крема (JS Big)"] == 20
    assert stock_by_title["Промокод -20%"] == 10
    assert stock_by_title["Комплимент стилиста"] is None

    _clear_brand_d_dependents(sync_engine)
    try:
        command.downgrade(cfg, PREVIOUS)
        assert _brand_d_activities(sync_engine) == {}
    finally:
        # База тестов общая для сессии: при сбое отката возвращаем схему и сиды в head.
        command.upgrade(cfg, "head")
    assert set(_brand_d_activities(sync_engine)) == BRAND_D_SLUGS
    assert {row.title for row in _brand_d_prizes(sync_engine)} == PRIZE_TITLES
