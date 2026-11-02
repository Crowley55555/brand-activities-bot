"""Содержимое и идемпотентность сид-миграций 0003/0004 (правило 5: данные - в БД)."""

from __future__ import annotations

import importlib.util
import itertools
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.activities.survey.rules import compute_condition
from app.core.catalog import get_recommendations_by_condition
from app.core.types.dto import SurveyAnswerRef
from app.core.types.ids import AttemptId, SurveyQuestionId
from tests.factories.survey import SURVEY_SLUG, UowFactory

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
ANSWERED_AT = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)


def _rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


def _load_migration(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_brand_a_activity_is_seeded_with_settings(sync_engine: Engine) -> None:
    rows = _rows(
        sync_engine,
        "SELECT a.slug, a.display_name, a.kind, a.is_active, a.deleted_at, a.limits, "
        "b.slug AS brand "
        "FROM activities a JOIN brands b ON b.id = a.brand_id "
        "WHERE a.slug = :slug",
        slug=SURVEY_SLUG,
    )

    # Фильтр по slug: с фазы 3a в сиде есть и активность Бренд B (0007), это не регрессия.
    assert len(rows) == 1
    activity = rows[0]
    assert activity.slug == SURVEY_SLUG
    assert activity.kind == "survey"
    assert activity.is_active is True and activity.deleted_at is None
    assert activity.brand == "brand_a"
    assert activity.limits["max_per_day"] == 1
    assert activity.limits["requires_moderation"] is False
    assert activity.limits["promocode_template"] == "BRAND_A-{random8}"
    assert activity.limits["max_recommendations"] == 3


def test_five_brands_are_still_seeded(sync_engine: Engine) -> None:
    assert len(_rows(sync_engine, "SELECT id FROM brands")) == 5


def test_questions_are_single_choice_with_expected_option_keys(sync_engine: Engine) -> None:
    rows = _rows(
        sync_engine,
        "SELECT position, question_type, options, deleted_at FROM survey_questions "
        "WHERE activity_id = (SELECT id FROM activities WHERE slug = :slug) ORDER BY position",
        slug=SURVEY_SLUG,
    )

    assert [r.position for r in rows] == [1, 2, 3, 4, 5]
    assert {r.question_type for r in rows} == {"single_choice"}
    assert all(r.deleted_at is None for r in rows)
    keys = [[o["key"] for o in r.options] for r in rows]
    assert keys == [
        ["combo", "dry", "oily", "sensitive"],
        ["age_18_25", "age_26_35", "age_36_plus"],
        ["yes", "no"],
        ["yes", "no"],
        ["yes", "no"],
    ]
    for row in rows:
        for option in row.options:
            assert option["label"].strip()
            assert option["profile"], "каждый вариант вносит вклад в профиль"
            assert all(isinstance(v, str) for v in option["profile"].values())


def test_each_question_sets_one_attribute_across_all_its_options(sync_engine: Engine) -> None:
    rows = _rows(
        sync_engine,
        "SELECT position, options FROM survey_questions "
        "WHERE activity_id = (SELECT id FROM activities WHERE slug = :slug) ORDER BY position",
        slug=SURVEY_SLUG,
    )

    attributes = []
    for row in rows:
        per_option = {tuple(o["profile"]) for o in row.options}
        assert len(per_option) == 1, f"вопрос {row.position}: варианты задают разные атрибуты"
        attributes.append(next(iter(per_option))[0])
    assert len(set(attributes)) == len(attributes)


def test_product_cards_are_seeded_with_valid_structure(sync_engine: Engine) -> None:
    cards = _rows(
        sync_engine,
        "SELECT position, condition, product_name, caption, product_url, is_active, deleted_at "
        "FROM product_recommendations "
        "WHERE activity_id = (SELECT id FROM activities WHERE slug = :slug) ORDER BY position",
        slug=SURVEY_SLUG,
    )

    assert len(cards) >= 6
    assert len({c.position for c in cards}) == len(cards)
    assert len({c.product_name for c in cards}) == len(cards)
    for card in cards:
        assert isinstance(card.condition, dict)
        assert all(isinstance(v, str) for v in card.condition.values())
        assert card.product_name.strip() and card.caption
        assert card.product_url.startswith("https://")
        assert card.is_active is True and card.deleted_at is None


def test_card_conditions_only_use_attributes_and_values_the_survey_can_produce(
    sync_engine: Engine,
) -> None:
    """Сиды 0003 и 0004 согласованы: условие карточки достижимо ответами."""
    options = _rows(sync_engine, "SELECT options FROM survey_questions")
    producible: dict[str, set[str]] = {}
    for row in options:
        for option in row.options:
            for attribute, value in option["profile"].items():
                producible.setdefault(attribute, set()).add(value)

    for card in _rows(sync_engine, "SELECT condition, product_name FROM product_recommendations"):
        for attribute, value in card.condition.items():
            assert value in producible.get(attribute, set()), (card.product_name, attribute, value)


async def test_every_possible_answer_combination_gets_between_one_and_three_cards(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    async with uow_factory() as uow:
        catalog = uow.repositories.catalog
        activity = await catalog.find_activity_by_slug(SURVEY_SLUG)
        assert activity is not None
        questions = await catalog.list_survey_questions(activity.id)
        limit = activity.limits["max_recommendations"]

        combos = list(itertools.product(*[q.options for q in questions]))
        assert len(combos) == 4 * 3 * 2 * 2 * 2
        for combo in combos:
            answers = [
                SurveyAnswerRef(
                    attempt_id=AttemptId(1),
                    question_id=SurveyQuestionId(question.id),
                    selected_option_keys=(option.key,),
                    free_text=None,
                    answered_at=ANSWERED_AT,
                )
                for question, option in zip(questions, combo, strict=True)
            ]
            profile = compute_condition(questions, answers)
            selected = (await get_recommendations_by_condition(catalog, activity.id, profile))[
                :limit
            ]
            assert 1 <= len(selected) <= 3, f"профиль без нормального набора: {profile}"


@pytest.mark.parametrize("name", ["0003_seed_brand_a_survey", "0004_seed_brand_a_products"])
def test_seed_migrations_are_idempotent(name: str, sync_engine: Engine) -> None:
    """Повторный запуск upgrade() ничего не добавляет (ON CONFLICT / NOT EXISTS)."""
    module = _load_migration(name)
    tables = ("activities", "survey_questions", "product_recommendations")
    with sync_engine.connect() as conn:
        outer = conn.begin()
        try:
            before = {
                t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in tables
            }
            context = MigrationContext.configure(conn)
            with Operations.context(context):
                module.upgrade()
                module.upgrade()
            after = {
                t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in tables
            }
        finally:
            outer.rollback()

    assert after == before


def test_revision_chain_is_linear_after_brands() -> None:
    survey = _load_migration("0003_seed_brand_a_survey")
    products = _load_migration("0004_seed_brand_a_products")

    assert survey.down_revision == "0002_seed_brands"
    assert products.down_revision == survey.revision
