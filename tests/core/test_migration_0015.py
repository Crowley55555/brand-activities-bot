"""Миграция 0015 (фаза 6, Бренд E): сид активностей, вопросов и карточек; up/down/up.

Решения ревью фазы 6: ADR-44 (только данные, кода нет), B1 (ваучера у Бренд E нет: `voucher_title`
= null, `voucher_code_template` не задан), B2 (ноты - bool-атрибуты `note_<x>: "yes"`, условия
карточек - скалярные пары). Брендов миграция не меняет: `brand_e` заведён в 0002.

База тестов общая для сессии, поэтому перед откатом чистим гостей (`clean_db`): прохождения Бренд E
держат FK RESTRICT. На проде downgrade 0015 не запускаем (как 0003/0004).
"""

from __future__ import annotations

from typing import Any

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from alembic import command

PREVIOUS = "0014_seed_brand_d"
SURVEY_SLUG = "brand_e_scent_match"
PHOTO_SLUG = "brand_e_photo"
BRANDS = {"brand_a", "brand_b", "brand_c", "brand_d", "brand_e"}
QUESTION_TYPES = [
    "single_choice",
    "single_choice",
    "multi_choice",
    "single_choice",
    "single_choice",
]
NOTE_ATTRIBUTES = {"note_citrus", "note_floral", "note_woody", "note_oriental", "note_gourmand"}


def _brand_e_activities(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.slug, a.kind, a.limits, b.slug AS brand_slug "
                "FROM activities a JOIN brands b ON b.id = a.brand_id "
                "WHERE a.slug IN (:survey, :photo)"
            ),
            {"survey": SURVEY_SLUG, "photo": PHOTO_SLUG},
        ).all()
    return {str(row.slug): row for row in rows}


def _questions(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT q.position, q.question_type, q.options FROM survey_questions q "
                    "JOIN activities a ON a.id = q.activity_id "
                    "WHERE a.slug = :slug ORDER BY q.position"
                ),
                {"slug": SURVEY_SLUG},
            ).all()
        )


def _cards(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT p.position, p.condition, p.product_name "
                    "FROM product_recommendations p JOIN activities a ON a.id = p.activity_id "
                    "WHERE a.slug = :slug ORDER BY p.position"
                ),
                {"slug": SURVEY_SLUG},
            ).all()
        )


def _brand_slugs(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return {str(row.slug) for row in conn.execute(text("SELECT slug FROM brands")).all()}


def test_migration_0015_up_down_up(pg_url: str, sync_engine: Engine, clean_db: None) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)

    activities = _brand_e_activities(sync_engine)
    assert set(activities) == {SURVEY_SLUG, PHOTO_SLUG}
    survey, photo = activities[SURVEY_SLUG], activities[PHOTO_SLUG]
    assert survey.kind == "survey"
    assert photo.kind == "ugc"
    assert survey.brand_slug == "brand_e"
    assert photo.brand_slug == "brand_e"
    assert survey.limits["max_recommendations"] == 3
    assert survey.limits["promocode_template"] == "BRAND_E-{random8}"
    assert photo.limits["promocode_template"] == "BRAND_E-{random8}"
    assert photo.limits["requires_moderation"] is True
    # B1: у Бренд E нет ваучера. `voucher_title` - null, `voucher_code_template` не задан.
    assert photo.limits["voucher_title"] is None
    assert "voucher_code_template" not in photo.limits

    questions = _questions(sync_engine)
    assert [q.position for q in questions] == [1, 2, 3, 4, 5]
    assert [q.question_type for q in questions] == QUESTION_TYPES
    assert all(option["profile"] for q in questions for option in q.options)
    notes = next(q for q in questions if q.question_type == "multi_choice")
    assert {attr for option in notes.options for attr in option["profile"]} == NOTE_ATTRIBUTES
    assert {value for option in notes.options for value in option["profile"].values()} == {"yes"}

    cards = _cards(sync_engine)
    assert [card.position for card in cards] == list(range(1, 11))
    # B2: условие карточки - скалярные пары атрибут -> строка, списков нет.
    assert all(isinstance(value, str) for card in cards for value in card.condition.values())
    assert _brand_slugs(sync_engine) == BRANDS

    try:
        command.downgrade(cfg, PREVIOUS)
        assert _brand_e_activities(sync_engine) == {}
        assert _questions(sync_engine) == []
        assert _cards(sync_engine) == []
        assert _brand_slugs(sync_engine) == BRANDS
    finally:
        # База тестов общая для сессии: при сбое отката возвращаем схему и сиды в head.
        command.upgrade(cfg, "head")
    assert set(_brand_e_activities(sync_engine)) == {SURVEY_SLUG, PHOTO_SLUG}
    assert len(_cards(sync_engine)) == 10
    assert _brand_slugs(sync_engine) == BRANDS
