"""Ответы и завершение опроса на настоящей БД: валидация, повторы, настройки активности."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.common.errors import ConflictError, NotFound, ValidationError
from tests.conftest import ManualClock
from tests.factories import create_guest
from tests.factories.survey import (
    COMBO_PICKS,
    DRY_MATURE_PICKS,
    PROMO_RE,
    SURVEY_SLUG,
    UowFactory,
    answer,
    answer_all,
    finish,
    run_survey,
    start,
)


def _rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


@pytest.fixture
def restore_seeds(sync_engine: Engine) -> Iterator[None]:
    """Тесты правят настройки активности и карточки; возвращаем сид как был."""
    limits = _rows(sync_engine, "SELECT id, limits FROM activities")
    cards = _rows(sync_engine, "SELECT id, is_active FROM product_recommendations")
    yield
    with sync_engine.begin() as conn:
        for row in limits:
            conn.execute(
                text("UPDATE activities SET limits = CAST(:l AS jsonb) WHERE id = :id"),
                {"l": json.dumps(row.limits), "id": row.id},
            )
        for card in cards:
            conn.execute(
                text("UPDATE product_recommendations SET is_active = :a WHERE id = :id"),
                {"a": card.is_active, "id": card.id},
            )


# ------------------------------------------------------------------- старт


async def test_start_returns_first_question_with_options(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    guest = create_guest(sync_engine)

    started = await start(uow_factory, guest, manual_clock)

    assert started.kind == "survey"
    assert started.activity_slug == SURVEY_SLUG
    assert started.total_questions == 5
    first = started.next_question
    assert first is not None
    assert first.position == 1
    assert first.question_type == "single_choice"
    assert [o.key for o in first.options] == ["combo", "dry", "oily", "sensitive"]
    assert all(o.label for o in first.options)


async def test_start_unknown_activity_is_not_found(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    guest = create_guest(sync_engine)

    with pytest.raises(NotFound) as exc:
        await start(uow_factory, guest, manual_clock, slug="no_such_survey")

    assert exc.value.code == "activity_not_found"


# ----------------------------------------------------------------- ответы


async def _started(
    uow_factory: UowFactory, sync_engine: Engine, clock: ManualClock
) -> tuple[int, Any]:
    guest = create_guest(sync_engine)
    started = await start(uow_factory, guest, clock)
    assert started.next_question is not None
    return started.attempt_id, started.next_question


@pytest.mark.parametrize(
    ("keys", "free_text"),
    [([], None), (["nope"], None), (["combo", "dry"], None), (["combo"], "текст")],
)
async def test_invalid_answer_is_rejected_and_not_stored(
    keys: list[str],
    free_text: str | None,
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
) -> None:
    attempt_id, question = await _started(uow_factory, sync_engine, manual_clock)

    with pytest.raises(ValidationError) as exc:
        await answer(uow_factory, attempt_id, question.id, keys, manual_clock, free_text)

    assert exc.value.code == "answer_invalid"
    assert _rows(sync_engine, "SELECT id FROM survey_answers") == []


async def test_answer_stores_selected_keys_and_returns_next_question(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    attempt_id, question = await _started(uow_factory, sync_engine, manual_clock)

    result = await answer(uow_factory, attempt_id, question.id, ["dry"], manual_clock)

    stored = _rows(
        sync_engine, "SELECT question_id, selected_option_keys, free_text FROM survey_answers"
    )
    assert len(stored) == 1
    assert stored[0].question_id == question.id
    assert stored[0].selected_option_keys == ["dry"]
    assert stored[0].free_text is None
    assert result.is_last is False
    assert (result.answered_count, result.total_questions) == (1, 5)
    assert result.next_question is not None
    assert result.next_question.position == 2


async def test_answering_the_same_question_twice_keeps_the_first_answer(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    """Двойной клик по кнопке: второй ответ отбивается UNIQUE, первый остаётся."""
    attempt_id, question = await _started(uow_factory, sync_engine, manual_clock)
    await answer(uow_factory, attempt_id, question.id, ["dry"], manual_clock)

    with pytest.raises(ConflictError) as exc:
        await answer(uow_factory, attempt_id, question.id, ["oily"], manual_clock)

    assert exc.value.code == "already_answered"
    stored = _rows(sync_engine, "SELECT selected_option_keys FROM survey_answers")
    assert [r.selected_option_keys for r in stored] == [["dry"]]


async def test_answer_for_unknown_question_is_not_found(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    attempt_id, _question = await _started(uow_factory, sync_engine, manual_clock)

    with pytest.raises(NotFound) as exc:
        await answer(uow_factory, attempt_id, 999_999, ["dry"], manual_clock)

    assert exc.value.code == "question_not_found"


async def test_answer_for_unknown_attempt_is_not_found(
    uow_factory: UowFactory, manual_clock: ManualClock, clean_db: None
) -> None:
    with pytest.raises(NotFound) as exc:
        await answer(uow_factory, 999_999, 1, ["dry"], manual_clock)

    assert exc.value.code == "attempt_not_found"


async def test_answer_for_question_of_another_activity_is_not_found(
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
) -> None:
    """Вопрос чужой активности не принимается, даже если id существует."""
    attempt_id, _question = await _started(uow_factory, sync_engine, manual_clock)
    with sync_engine.begin() as conn:
        foreign_activity = conn.execute(
            text(
                "INSERT INTO activities (brand_id, slug, display_name, kind) "
                "SELECT id, 'tmp_foreign_survey', 'tmp', 'survey' FROM brands LIMIT 1 "
                "RETURNING id"
            )
        ).scalar_one()
        foreign_question = conn.execute(
            text(
                "INSERT INTO survey_questions "
                "(activity_id, position, text, question_type, options) "
                "VALUES (:a, 1, 'чужой вопрос', 'single_choice', "
                '        CAST(\'[{"key":"x","label":"X"}]\' AS jsonb)) RETURNING id'
            ),
            {"a": foreign_activity},
        ).scalar_one()
    try:
        with pytest.raises(NotFound) as exc:
            await answer(uow_factory, attempt_id, foreign_question, ["x"], manual_clock)
        assert exc.value.code == "question_not_found"
    finally:
        with sync_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM survey_questions WHERE activity_id = :a"), {"a": foreign_activity}
            )
            conn.execute(text("DELETE FROM activities WHERE id = :a"), {"a": foreign_activity})


async def test_answer_after_finish_is_rejected(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    guest = create_guest(sync_engine)
    started = await start(uow_factory, guest, manual_clock)
    first = started.next_question
    assert first is not None
    await answer_all(uow_factory, started, COMBO_PICKS, manual_clock)
    await finish(uow_factory, started.attempt_id, manual_clock)

    with pytest.raises(ConflictError) as exc:
        await answer(uow_factory, started.attempt_id, first.id, ["combo"], manual_clock)

    assert exc.value.code == "already_closed"


# ------------------------------------------------------------- завершение


async def test_finish_before_all_questions_answered_is_incomplete(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    attempt_id, question = await _started(uow_factory, sync_engine, manual_clock)
    await answer(uow_factory, attempt_id, question.id, ["combo"], manual_clock)

    with pytest.raises(ConflictError) as exc:
        await finish(uow_factory, attempt_id, manual_clock)

    assert exc.value.code == "survey_incomplete"
    row = _rows(sync_engine, "SELECT status FROM attempts")[0]
    assert row.status == "in_progress"
    assert _rows(sync_engine, "SELECT id FROM rewards") == []


async def test_finish_twice_second_call_is_rejected_and_rewards_stay(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    guest = create_guest(sync_engine)
    result = await run_survey(uow_factory, guest, manual_clock, COMBO_PICKS)
    before = _rows(sync_engine, "SELECT id, reward_type, code FROM rewards ORDER BY id")

    with pytest.raises(ConflictError) as exc:
        await finish(uow_factory, result.attempt_id, manual_clock)

    assert exc.value.code == "already_closed"
    after = _rows(sync_engine, "SELECT id, reward_type, code FROM rewards ORDER BY id")
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


async def test_finish_unknown_attempt_is_not_found(
    uow_factory: UowFactory, manual_clock: ManualClock, clean_db: None
) -> None:
    with pytest.raises(NotFound) as exc:
        await finish(uow_factory, 999_999, manual_clock)

    assert exc.value.code == "attempt_not_found"


async def test_summary_has_profile_slugs_and_no_personal_data(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock, clean_db: None
) -> None:
    guest = create_guest(sync_engine)
    await run_survey(uow_factory, guest, manual_clock, COMBO_PICKS)

    summary = _rows(sync_engine, "SELECT summary FROM attempts")[0].summary

    assert summary == "age=26_35; oily_tzone=no; pigmentation=no; sensitivity=no; skin_type=combo"


# ------------------------------------- настройки активности (limits из сида)


async def test_max_recommendations_limit_from_activity_settings(
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
    restore_seeds: None,
) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE activities SET limits = limits || "
                "CAST('{\"max_recommendations\": 1}' AS jsonb) "
                "WHERE slug = :s"
            ),
            {"s": SURVEY_SLUG},
        )
    guest = create_guest(sync_engine)

    result = await run_survey(uow_factory, guest, manual_clock, COMBO_PICKS)

    assert len(result.recommendations) == 1
    stored = _rows(sync_engine, "SELECT product_recommendation_ids FROM rewards WHERE code IS NULL")
    assert len(stored[0].product_recommendation_ids) == 1


async def test_default_limit_caps_the_set_at_three_cards(
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
    restore_seeds: None,
) -> None:
    guest = create_guest(sync_engine)

    result = await run_survey(uow_factory, guest, manual_clock, DRY_MATURE_PICKS)

    # Подходят пять карточек (два признака + четыре одиночных), лимит сида - три.
    assert len(result.recommendations) == 3


async def test_activity_without_promocode_template_gives_no_promocode(
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
    restore_seeds: None,
) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE activities SET limits = limits - 'promocode_template' WHERE slug = :s"),
            {"s": SURVEY_SLUG},
        )
    guest = create_guest(sync_engine)

    result = await run_survey(uow_factory, guest, manual_clock, COMBO_PICKS)

    assert result.promocode is None
    types = [r.reward_type for r in _rows(sync_engine, "SELECT reward_type FROM rewards")]
    assert types == ["recommendation_set"]


async def test_survey_without_matching_cards_still_gives_promocode(
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
    restore_seeds: None,
) -> None:
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE product_recommendations SET is_active = FALSE"))
    guest = create_guest(sync_engine)

    result = await run_survey(uow_factory, guest, manual_clock, COMBO_PICKS)

    assert result.recommendations == ()
    assert PROMO_RE.fullmatch(result.promocode or "")
    types = [r.reward_type for r in _rows(sync_engine, "SELECT reward_type FROM rewards")]
    assert types == ["promocode"]


async def test_deactivated_activity_does_not_block_finishing_started_attempt(
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
    restore_seeds: None,
) -> None:
    """Гость начал опрос, активность выключили: начатое прохождение можно закончить."""
    guest = create_guest(sync_engine)
    started = await start(uow_factory, guest, manual_clock)
    await answer_all(uow_factory, started, COMBO_PICKS, manual_clock)
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE activities SET is_active = FALSE WHERE slug = :s"), {"s": SURVEY_SLUG}
        )
    try:
        result = await finish(uow_factory, started.attempt_id, manual_clock)
        assert PROMO_RE.fullmatch(result.promocode or "")
    finally:
        with sync_engine.begin() as conn:
            conn.execute(
                text("UPDATE activities SET is_active = TRUE WHERE slug = :s"), {"s": SURVEY_SLUG}
            )
