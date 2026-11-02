"""Сценарий опроса Бренд A для тестов: старт, ответы, завершение.

Каждый шаг идёт в своей транзакции (`async with uow_factory()`), как отдельный
HTTP-запрос tg_worker к ядру. Названия активности и ключи ответов здесь - только
тестовые данные; в `app/` и `tg_worker/` их быть не должно (правило 5).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from app.core.activities.survey import finish_survey, start_survey, submit_answer
from app.core.ports.clock import ClockPort
from app.core.types.dto import SurveyQuestionRef
from app.core.types.ids import AttemptId, ChannelAccountId, SurveyQuestionId, UserId
from app.core.types.results import (
    FinishSurveyResult,
    StartAttemptResult,
    SubmitAnswerResult,
)
from app.db.unit_of_work import UnitOfWork
from tests.factories import Guest

SURVEY_SLUG = "brand_a_skin_survey"
PROMO_TEMPLATE = "BRAND_A-{random8}"
PROMO_RE = re.compile(r"^BRAND_A-[A-Z0-9]{8}$")

# Порядок ключей = порядок вопросов сида 0003: тип кожи, возраст,
# чувствительность, жирность Т-зоны, пигментация.
COMBO_PICKS = ["combo", "age_26_35", "no", "no", "no"]
DRY_MATURE_PICKS = ["dry", "age_36_plus", "yes", "yes", "yes"]

UowFactory = Callable[[], UnitOfWork]


async def start(
    uow_factory: UowFactory, guest: Guest, clock: ClockPort, slug: str = SURVEY_SLUG
) -> StartAttemptResult:
    async with uow_factory() as uow:
        r = uow.repositories
        return await start_survey(
            r.consents,
            r.attempts,
            r.catalog,
            UserId(guest.user_id),
            ChannelAccountId(guest.channel_account_id),
            slug,
            clock.now(),
        )


async def answer(
    uow_factory: UowFactory,
    attempt_id: int,
    question_id: int,
    keys: list[str],
    clock: ClockPort,
    free_text: str | None = None,
) -> SubmitAnswerResult:
    async with uow_factory() as uow:
        r = uow.repositories
        return await submit_answer(
            r.consents,
            r.attempts,
            r.catalog,
            r.surveys,
            AttemptId(attempt_id),
            SurveyQuestionId(question_id),
            keys,
            free_text,
            clock.now(),
        )


async def finish(
    uow_factory: UowFactory,
    attempt_id: int,
    clock: ClockPort,
    random_source: Callable[[int], str] | None = None,
) -> FinishSurveyResult:
    async with uow_factory() as uow:
        r = uow.repositories
        return await finish_survey(
            r.consents,
            r.attempts,
            r.catalog,
            r.surveys,
            r.rewards,
            AttemptId(attempt_id),
            clock.now(),
            random_source,
        )


async def answer_all(
    uow_factory: UowFactory,
    started: StartAttemptResult,
    picks: list[str],
    clock: ClockPort,
) -> None:
    question: SurveyQuestionRef | None = started.next_question
    for key in picks:
        assert question is not None, "вопросов меньше, чем ответов в сценарии"
        result = await answer(uow_factory, started.attempt_id, question.id, [key], clock)
        question = result.next_question
    assert question is None, "остались неотвеченные вопросы"


async def run_survey(
    uow_factory: UowFactory,
    guest: Guest,
    clock: ClockPort,
    picks: list[str],
    random_source: Callable[[int], str] | None = None,
) -> FinishSurveyResult:
    started = await start(uow_factory, guest, clock)
    await answer_all(uow_factory, started, picks, clock)
    return await finish(uow_factory, started.attempt_id, clock, random_source)
