from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from app.common.errors import ConflictError, NotFound, ValidationError
from app.core.activities.survey.repository import SurveyAnswerRepo
from app.core.activities.survey.rules import compute_condition, summarize_profile
from app.core.attempts import AttemptRepo, close_attempt, get_attempt, start_attempt
from app.core.catalog import (
    CatalogRepo,
    get_activity_by_id,
    get_recommendations_by_condition,
    get_survey_questions,
)
from app.core.consent import ConsentRepo, require_consent
from app.core.rewards import RewardRepo, grant_on_completion
from app.core.rewards.code_generator import RandomSource
from app.core.types.dto import GrantSpec, SurveyQuestionRef
from app.core.types.ids import (
    AttemptId,
    ChannelAccountId,
    SurveyQuestionId,
    UserId,
)
from app.core.types.results import (
    FinishSurveyResult,
    StartAttemptResult,
    SubmitAnswerResult,
)

_MAX_FREE_TEXT_LENGTH: Final[int] = 1000

# Ключи activities.limits, которые читает движок опроса (сид-миграция активности).
_LIMIT_PROMOCODE_TEMPLATE: Final[str] = "promocode_template"
_LIMIT_MAX_RECOMMENDATIONS: Final[str] = "max_recommendations"


def _limit_text(limits: Mapping[str, Any], key: str) -> str | None:
    value = limits.get(key)
    return value if isinstance(value, str) and value else None


def _limit_positive_int(limits: Mapping[str, Any], key: str) -> int | None:
    value = limits.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _validate_answer(
    question: SurveyQuestionRef,
    selected_option_keys: Sequence[str],
    free_text: str | None,
) -> tuple[tuple[str, ...], str | None]:
    """Привести ответ к хранимому виду или бросить ValidationError('answer_invalid')."""
    if question.question_type == "free_text":
        text = (free_text or "").strip()
        if not text or len(text) > _MAX_FREE_TEXT_LENGTH or selected_option_keys:
            raise ValidationError("answer_invalid")
        return (), text

    keys = tuple(selected_option_keys)
    allowed = {option.key for option in question.options}
    if (
        free_text
        or not keys
        or len(set(keys)) != len(keys)
        or any(key not in allowed for key in keys)
        or (question.question_type == "single_choice" and len(keys) != 1)
    ):
        raise ValidationError("answer_invalid")
    return keys, None


async def start_survey(
    consents: ConsentRepo,
    attempts: AttemptRepo,
    catalog: CatalogRepo,
    user_id: UserId,
    channel_account_id: ChannelAccountId,
    activity_slug: str,
    now: datetime,
) -> StartAttemptResult:
    """Начать опрос: возвращает прохождение и первый вопрос.

    Правило 1 (consent-gate): первой строкой - `require_consent`; без согласия
    ConflictError('consent_required') и ни одной записи. Дальше `start_attempt` проверит
    активность и «один раз в день» на стороне БД.
    """
    await require_consent(consents, user_id)
    started = await start_attempt(
        consents,
        catalog,
        attempts,
        user_id,
        activity_slug,
        channel_account_id,
        now,
        expected_kind="survey",
    )
    questions = await get_survey_questions(catalog, started.activity_id)
    if not questions:
        raise NotFound("survey_questions_not_found")  # откатит и вставку попытки
    return StartAttemptResult(
        attempt_id=started.attempt_id,
        activity_id=started.activity_id,
        activity_slug=started.activity_slug,
        kind=started.kind,
        status=started.status,
        attempt_date=started.attempt_date,
        next_question=questions[0],
        total_questions=len(questions),
    )


async def submit_answer(
    consents: ConsentRepo,
    attempts: AttemptRepo,
    catalog: CatalogRepo,
    answers: SurveyAnswerRepo,
    attempt_id: AttemptId,
    question_id: SurveyQuestionId,
    selected_option_keys: Sequence[str],
    free_text: str | None,
    now: datetime,
) -> SubmitAnswerResult:
    """Записать ответ и вернуть следующий неотвеченный вопрос (или is_last=True).

    Правило 1 (consent-gate): владелец берётся из прохождения, и согласие проверяется до
    любой записи. После отзыва согласия (/forget) ответ не пишется: consent_required.
    """
    attempt = await get_attempt(attempts, attempt_id)
    await require_consent(consents, attempt.user_id)
    if attempt.status != "in_progress":
        raise ConflictError("already_closed")
    questions = await get_survey_questions(catalog, attempt.activity_id)
    question = next((q for q in questions if q.id == question_id), None)
    if question is None:
        raise NotFound("question_not_found")
    keys, text = _validate_answer(question, selected_option_keys, free_text)

    saved = await answers.insert(attempt_id, question_id, keys, text, now)
    if saved is None:
        raise ConflictError("already_answered")

    answered_ids = {a.question_id for a in await answers.list_for_attempt(attempt_id)}
    next_question = next((q for q in questions if q.id not in answered_ids), None)
    return SubmitAnswerResult(
        attempt_id=attempt_id,
        question_id=question_id,
        next_question=next_question,
        is_last=next_question is None,
        answered_count=sum(1 for q in questions if q.id in answered_ids),
        total_questions=len(questions),
    )


async def finish_survey(
    consents: ConsentRepo,
    attempts: AttemptRepo,
    catalog: CatalogRepo,
    answers: SurveyAnswerRepo,
    rewards: RewardRepo,
    attempt_id: AttemptId,
    now: datetime,
    random_source: RandomSource | None = None,
) -> FinishSurveyResult:
    """Завершить опрос: профиль -> карточки -> close_attempt -> награды, одной транзакцией.

    Транзакцию открывает вызывающий (UnitOfWork). Правило 1: согласие владельца проверяется
    до close_attempt и выдачи наград. Любая ошибка после `close_attempt`
    (дубль выдачи, исчерпанный ретрай кода) откатывает всё: прохождение остаётся
    in_progress, наград нет - повторная попытка начнётся с чистого состояния (04 §4.4).
    """
    attempt = await get_attempt(attempts, attempt_id)
    await require_consent(consents, attempt.user_id)
    if attempt.status != "in_progress":
        raise ConflictError("already_closed")
    activity = await get_activity_by_id(catalog, attempt.activity_id)
    if activity.kind != "survey":
        raise ValidationError("activity_kind_mismatch")

    questions = await get_survey_questions(catalog, activity.id)
    if not questions:
        raise NotFound("survey_questions_not_found")
    given = await answers.list_for_attempt(attempt_id)
    answered_ids = {a.question_id for a in given}
    if any(q.id not in answered_ids for q in questions):
        raise ConflictError("survey_incomplete")

    profile = compute_condition(questions, given)
    cards = await get_recommendations_by_condition(catalog, activity.id, profile)
    max_cards = _limit_positive_int(activity.limits, _LIMIT_MAX_RECOMMENDATIONS)
    if max_cards is not None:
        cards = cards[:max_cards]

    summary = summarize_profile(profile)
    await close_attempt(attempts, attempt_id, "completed", summary, now)

    grants: list[GrantSpec] = []
    if cards:
        grants.append(
            GrantSpec(
                reward_type="recommendation_set",
                product_recommendation_ids=[int(card.id) for card in cards],
            )
        )
    template = _limit_text(activity.limits, _LIMIT_PROMOCODE_TEMPLATE)
    if template is not None:
        grants.append(GrantSpec(reward_type="promocode", code_template=template))
    issued = (
        await grant_on_completion(rewards, attempt_id, grants, now, random_source) if grants else []
    )

    promocode = next((r.code for r in issued if r.reward_type == "promocode"), None)
    return FinishSurveyResult(
        attempt_id=attempt_id,
        summary=summary,
        profile=profile,
        recommendations=tuple(cards),
        rewards=tuple(issued),
        promocode=promocode,
    )
