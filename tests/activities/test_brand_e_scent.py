"""Приёмочные тесты фазы 6: Бренд E «Код настроения» (T1-T2, подбор аромата).

Опрос, вопросы и карточки берутся из сид-миграции 0015 (правило 5). Тесты идут через настоящее
ядро и настоящий Postgres, без моков (как test_brand_a_survey.py).

Ноты - multi_choice: один ответ несёт несколько ключей, а каждый ключ даёт свой скалярный атрибут
профиля `note_<x>: "yes"` (решение B2 ревью фазы 6). Так карточки матчатся парами, как и остальные.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.types.results import FinishSurveyResult
from tests.conftest import ManualClock
from tests.factories import Guest, create_guest
from tests.factories.survey import UowFactory, answer, finish, start

SCENT_SLUG = "brand_e_scent_match"
PROMO_RE = re.compile(r"^BRAND_E-[A-Z0-9]{8}$")

# Порядок = порядок вопросов сида 0015: настроение, впечатление, ноты (несколько), время, сезон.
PROFILE_A = [["calm"], ["fresh"], ["citrus", "floral"], ["day"], ["spring"]]
PROFILE_B = [["confident"], ["mysterious"], ["oriental", "woody"], ["evening"], ["winter"]]
# Та же анкета, что PROFILE_A, но сезон - зима: весенняя карточка Serenity не подходит (T4).
PROFILE_A_WINTER = [["calm"], ["fresh"], ["citrus", "floral"], ["day"], ["winter"]]
PROFILE_A_ATTRIBUTES = {
    "mood": "calm",
    "impression": "fresh",
    "note_citrus": "yes",
    "note_floral": "yes",
    "time": "day",
    "season": "spring",
}


def _rows(engine: Engine, sql: str) -> list:
    with engine.connect() as conn:
        return list(conn.execute(text(sql)).all())


async def _run_scent(
    uow_factory: UowFactory, guest: Guest, clock: ManualClock, picks: list[list[str]]
) -> FinishSurveyResult:
    """Старт, по одному ответу на каждый вопрос (ноты - одним ответом с несколькими ключами)."""
    started = await start(uow_factory, guest, clock, slug=SCENT_SLUG)
    question = started.next_question
    for keys in picks:
        assert question is not None, "вопросов меньше, чем ответов в сценарии"
        result = await answer(uow_factory, started.attempt_id, question.id, keys, clock)
        question = result.next_question
    assert question is None, "остались неотвеченные вопросы"
    return await finish(uow_factory, started.attempt_id, clock)


# ---------------------------------------------------------------- T1


async def test_brand_e_survey_profile_a_matches_scent_x(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    """T1: спокойное, свежее, цитрус+флораль -> набор карточек и промокод BRAND_E-."""
    guest = create_guest(sync_engine)

    result = await _run_scent(uow_factory, guest, manual_clock, PROFILE_A)

    assert dict(result.profile) == PROFILE_A_ATTRIBUTES
    rewards = {
        row.reward_type: row
        for row in _rows(
            sync_engine,
            "SELECT reward_type, code, product_recommendation_ids FROM rewards",
        )
    }
    assert set(rewards) == {"recommendation_set", "promocode"}

    ids = rewards["recommendation_set"].product_recommendation_ids
    assert ids, "набор рекомендаций не должен быть пустым"
    assert ids == [card.id for card in result.recommendations]
    # Минимум одна карточка совпадает по настроению и впечатлению.
    assert any(
        card.condition.get("mood") == "calm" and card.condition.get("impression") == "fresh"
        for card in result.recommendations
    ), result.recommendations

    code = rewards["promocode"].code
    assert PROMO_RE.fullmatch(code), code
    assert result.promocode == code


# ---------------------------------------------------------------- T2


async def test_brand_e_survey_profile_b_matches_different_scent(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    """T2: уверенное, таинственное, восточно-древесные -> другие карточки, чем у T1."""
    first = create_guest(sync_engine, "1001")
    second = create_guest(sync_engine, "1002")

    a = await _run_scent(uow_factory, first, manual_clock, PROFILE_A)
    b = await _run_scent(uow_factory, second, manual_clock, PROFILE_B)

    ids_a = [card.id for card in a.recommendations]
    ids_b = [card.id for card in b.recommendations]
    assert ids_a and ids_b
    assert ids_a != ids_b
    assert set(ids_a).isdisjoint(ids_b)
    # Самая специфичная карточка профиля B - восточно-древесный аромат (два совпавших ноты).
    assert b.recommendations[0].condition == {
        "mood": "confident",
        "note_oriental": "yes",
        "note_woody": "yes",
    }
    assert a.promocode != b.promocode


# ---------------------------------------------------------------- T4


async def test_brand_e_survey_season_changes_recommendations(
    uow_factory: UowFactory, sync_engine: Engine, manual_clock: ManualClock
) -> None:
    """T4: сезон влияет на подбор - та же анкета зимой не получает весеннюю карточку."""
    spring = create_guest(sync_engine, "1003")
    winter = create_guest(sync_engine, "1004")

    a = await _run_scent(uow_factory, spring, manual_clock, PROFILE_A)
    w = await _run_scent(uow_factory, winter, manual_clock, PROFILE_A_WINTER)

    assert a.recommendations[0].condition.get("season") == "spring"
    assert [card.id for card in a.recommendations] != [card.id for card in w.recommendations]
    assert all(card.condition.get("season") in (None, "winter") for card in w.recommendations)
