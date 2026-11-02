"""Приёмочные тесты фазы 2: Бренд A «Код кожи» (T1-T5).

Опрос, вопросы и карточки продуктов берутся из сид-миграций 0003/0004 (правило 5):
тесты идут через настоящее ядро и настоящий Postgres, без моков репозиториев.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.common.errors import ConflictError
from app.core.rewards import grant_on_completion
from app.core.types.dto import GrantSpec
from app.core.types.ids import AttemptId
from tests.conftest import ManualClock
from tests.factories import create_attempt, create_guest, revoke_consent
from tests.factories.survey import (
    COMBO_PICKS,
    DRY_MATURE_PICKS,
    PROMO_RE,
    PROMO_TEMPLATE,
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


def _count(engine: Engine, table: str) -> int:
    return int(_rows(engine, f"SELECT count(*) AS n FROM {table}")[0].n)


# ---------------------------------------------------------------- T1


async def test_start_without_consent_rejects(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    """Правило 1: без действующего согласия - 409 consent_required, ничего не пишем."""
    guest = create_guest(sync_engine, with_consent=False)

    with pytest.raises(ConflictError) as exc:
        await start(uow_factory, guest, manual_clock)

    assert exc.value.code == "consent_required"
    assert _count(sync_engine, "attempts") == 0
    assert _count(sync_engine, "survey_answers") == 0
    assert _count(sync_engine, "rewards") == 0


async def test_start_after_consent_revoked_rejects(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    guest = create_guest(sync_engine)
    revoke_consent(sync_engine, guest)

    with pytest.raises(ConflictError) as exc:
        await start(uow_factory, guest, manual_clock)

    assert exc.value.code == "consent_required"
    assert _count(sync_engine, "attempts") == 0


# ---------------------------------------------------------------- T2


async def test_full_survey_happy_path_grants_recommendations_and_promocode(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    guest = create_guest(sync_engine)

    result = await run_survey(uow_factory, guest, manual_clock, COMBO_PICKS)

    attempts = _rows(sync_engine, "SELECT status, summary, finished_at FROM attempts")
    assert len(attempts) == 1
    assert attempts[0].status == "completed"
    assert attempts[0].summary, "summary должен описывать профиль (слаги, без ПДн)"
    assert "skin_type=combo" in attempts[0].summary
    assert attempts[0].finished_at is not None

    answers = _rows(sync_engine, "SELECT question_id FROM survey_answers")
    assert len(answers) == len(COMBO_PICKS) == 5

    rewards = {
        row.reward_type: row
        for row in _rows(
            sync_engine,
            "SELECT reward_type, code, product_recommendation_ids FROM rewards",
        )
    }
    assert set(rewards) == {"recommendation_set", "promocode"}

    ids = rewards["recommendation_set"].product_recommendation_ids
    assert isinstance(ids, list)
    assert ids, "набор рекомендаций не должен быть пустым"
    assert all(isinstance(i, int) for i in ids)
    assert ids == [card.id for card in result.recommendations]
    # ADR-14: в награде лежат только id, а не содержимое карточек.
    assert rewards["recommendation_set"].product_recommendation_ids == ids

    code = rewards["promocode"].code
    assert PROMO_RE.fullmatch(code), code
    assert len(code) == len("BRAND_A-") + 8
    assert result.promocode == code
    assert rewards["recommendation_set"].code is None


async def test_happy_path_recommendations_match_profile(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    """Каждая выданная карточка должна подходить под профиль гостя."""
    guest = create_guest(sync_engine)
    result = await run_survey(uow_factory, guest, manual_clock, COMBO_PICKS)

    profile = {
        "skin_type": "combo",
        "age": "26_35",
        "sensitivity": "no",
        "oily_tzone": "no",
        "pigmentation": "no",
    }
    assert dict(result.profile) == profile
    assert result.recommendations
    for card in result.recommendations:
        assert all(profile.get(k) == v for k, v in card.condition.items()), card
        assert card.product_url.startswith("https://")
    # Самая подходящая карточка - под тип кожи.
    assert result.recommendations[0].condition.get("skin_type") == "combo"


# ---------------------------------------------------------------- T3


async def test_same_user_cannot_start_twice_same_day(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    guest = create_guest(sync_engine)
    await run_survey(uow_factory, guest, manual_clock, COMBO_PICKS)

    with pytest.raises(ConflictError) as exc:
        await start(uow_factory, guest, manual_clock)

    assert exc.value.code == "already_passed_today"
    assert _count(sync_engine, "attempts") == 1


async def test_abandoned_attempt_still_blocks_same_day(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    """in_progress входит в partial unique attempts_once_per_day (03 §2 Q3)."""
    guest = create_guest(sync_engine)
    await start(uow_factory, guest, manual_clock)

    with pytest.raises(ConflictError) as exc:
        await start(uow_factory, guest, manual_clock)

    assert exc.value.code == "already_passed_today"
    assert _count(sync_engine, "attempts") == 1


async def test_next_day_allows_new_attempt(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    guest = create_guest(sync_engine)
    await run_survey(uow_factory, guest, manual_clock, COMBO_PICKS)

    manual_clock.set(manual_clock.now() + timedelta(days=1))
    second = await start(uow_factory, guest, manual_clock)

    assert second.attempt_id
    assert _count(sync_engine, "attempts") == 2


async def test_other_user_can_start_same_day(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    first = create_guest(sync_engine, "1001")
    second = create_guest(sync_engine, "1002")

    await start(uow_factory, first, manual_clock)
    await start(uow_factory, second, manual_clock)

    assert _count(sync_engine, "attempts") == 2


async def test_expired_attempt_frees_the_day(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    """expired не входит в partial unique attempts_once_per_day: день свободен (ADR-19)."""
    guest = create_guest(sync_engine)
    started = await start(uow_factory, guest, manual_clock)
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE attempts SET status = 'expired' WHERE id = :id"),
            {"id": started.attempt_id},
        )

    again = await start(uow_factory, guest, manual_clock)

    assert again.attempt_id != started.attempt_id


# ---------------------------------------------------------------- T4


async def test_duplicate_grant_rejects_with_conflict(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    """Повтор выдачи на то же прохождение и тот же тип: ConflictError, без изменений (04 §4.4)."""
    guest = create_guest(sync_engine)
    result = await run_survey(uow_factory, guest, manual_clock, COMBO_PICKS)
    before = _rows(sync_engine, "SELECT id, reward_type, code FROM rewards ORDER BY id")
    assert len(before) == 2

    same_grants = [
        GrantSpec(
            reward_type="recommendation_set",
            product_recommendation_ids=[card.id for card in result.recommendations],
        ),
        GrantSpec(reward_type="promocode", code_template=PROMO_TEMPLATE),
    ]
    with pytest.raises(ConflictError) as exc:
        async with uow_factory() as uow:
            await grant_on_completion(
                uow.repositories.rewards,
                AttemptId(result.attempt_id),
                same_grants,
                manual_clock.now(),
            )

    assert exc.value.code == "duplicate_grant"
    after = _rows(sync_engine, "SELECT id, reward_type, code FROM rewards ORDER BY id")
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


async def test_brand_bed_promocode_only_grant_is_rejected_too(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    """Новый код не обходит защиту: уникальна пара (attempt_id, reward_type)."""
    guest = create_guest(sync_engine)
    result = await run_survey(uow_factory, guest, manual_clock, COMBO_PICKS)
    original = result.promocode

    with pytest.raises(ConflictError) as exc:
        async with uow_factory() as uow:
            await grant_on_completion(
                uow.repositories.rewards,
                AttemptId(result.attempt_id),
                [GrantSpec(reward_type="promocode", code_template=PROMO_TEMPLATE)],
                manual_clock.now(),
            )

    assert exc.value.code == "duplicate_grant"
    codes = [r.code for r in _rows(sync_engine, "SELECT code FROM rewards WHERE code IS NOT NULL")]
    assert codes == [original]


# ---------------------------------------------------------------- T5


async def test_different_profiles_give_different_recommendations(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    combo_guest = create_guest(sync_engine, "1001")
    dry_guest = create_guest(sync_engine, "1002")

    combo = await run_survey(uow_factory, combo_guest, manual_clock, COMBO_PICKS)
    dry = await run_survey(uow_factory, dry_guest, manual_clock, DRY_MATURE_PICKS)

    combo_ids = [card.id for card in combo.recommendations]
    dry_ids = [card.id for card in dry.recommendations]
    assert combo_ids and dry_ids
    assert combo_ids != dry_ids
    assert dict(combo.profile) != dict(dry.profile)

    stored = {
        row.user_id: row.product_recommendation_ids
        for row in _rows(
            sync_engine,
            "SELECT a.user_id, r.product_recommendation_ids "
            "FROM rewards r JOIN attempts a ON a.id = r.attempt_id "
            "WHERE r.reward_type = 'recommendation_set'",
        )
    }
    assert stored[combo_guest.user_id] == combo_ids
    assert stored[dry_guest.user_id] == dry_ids
    assert combo.promocode != dry.promocode

    # Для зрелой сухой кожи самая специфичная карточка (два признака) идёт первой.
    assert dry.recommendations[0].condition == {"skin_type": "dry", "age": "36_plus"}


# ----------------------------------------------- ответы и атомарность


async def test_attempt_date_is_moscow_date(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    """22:30 UTC 12 октября - уже 13 октября по Москве (CHECK attempts_date_matches_started)."""
    manual_clock.set(datetime(2026, 10, 12, 22, 30, tzinfo=UTC))
    guest = create_guest(sync_engine)

    await start(uow_factory, guest, manual_clock)

    rows = _rows(sync_engine, "SELECT attempt_date::text AS d FROM attempts")
    assert [r.d for r in rows] == ["2026-10-13"]


async def test_failed_grant_rolls_back_the_whole_finish(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    """close_attempt и grant_on_completion - одна транзакция (04 §4.4, п. 3).

    Код промокода «занят» чужой наградой, генератор всегда отдаёт тот же код:
    после 5 попыток finish падает, а попытка остаётся in_progress без наград.
    """
    other = create_guest(sync_engine, "2002")
    other_attempt = create_attempt(sync_engine, other, SURVEY_SLUG)
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rewards (attempt_id, reward_type, code, issued_at) "
                "VALUES (:aid, 'promocode', 'BRAND_A-AAAAAAAA', now())"
            ),
            {"aid": other_attempt},
        )

    guest = create_guest(sync_engine, "1001")
    started = await start(uow_factory, guest, manual_clock)
    await answer_all(uow_factory, started, COMBO_PICKS, manual_clock)

    with pytest.raises(ConflictError) as exc:
        await finish(
            uow_factory, started.attempt_id, manual_clock, random_source=lambda n: "A" * n
        )

    assert exc.value.code == "code_collision"
    row = _rows(
        sync_engine,
        "SELECT status, finished_at FROM attempts WHERE id = :id",
        id=started.attempt_id,
    )[0]
    assert row.status == "in_progress"
    assert row.finished_at is None
    assert _count(sync_engine, "rewards") == 1  # только чужая награда

    # Повтор с нормальным генератором проходит: попытка не «залипла» в completed.
    retry = await finish(uow_factory, started.attempt_id, manual_clock)
    assert PROMO_RE.fullmatch(retry.promocode or "")


# ------------------------------------------ отзыв согласия посреди опроса (правило 1)


async def test_submit_answer_rejects_after_consent_revoke(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    """/forget посреди опроса: следующий ответ не пишется, гость получает consent_required."""
    guest = create_guest(sync_engine)
    started = await start(uow_factory, guest, manual_clock)
    revoke_consent(sync_engine, guest)

    with pytest.raises(ConflictError) as exc:
        await answer(
            uow_factory,
            started.attempt_id,
            started.next_question.id,
            [COMBO_PICKS[0]],
            manual_clock,
        )

    assert exc.value.code == "consent_required"
    assert _count(sync_engine, "survey_answers") == 0
    assert _count(sync_engine, "rewards") == 0


async def test_finish_survey_rejects_after_consent_revoke(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    """Все ответы даны, согласие отозвано до финиша: прохождение не закрыто, наград нет."""
    guest = create_guest(sync_engine)
    started = await start(uow_factory, guest, manual_clock)
    await answer_all(uow_factory, started, COMBO_PICKS, manual_clock)
    revoke_consent(sync_engine, guest)

    with pytest.raises(ConflictError) as exc:
        await finish(uow_factory, started.attempt_id, manual_clock)

    assert exc.value.code == "consent_required"
    row = _rows(
        sync_engine,
        "SELECT status, finished_at FROM attempts WHERE id = :id",
        id=started.attempt_id,
    )[0]
    assert row.status == "in_progress"
    assert row.finished_at is None
    assert _count(sync_engine, "rewards") == 0
