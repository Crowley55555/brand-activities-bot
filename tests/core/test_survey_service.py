"""Сервис опроса на фейковых репозиториях: порядок вызовов и ветвление (правило 1, ADR-12/13)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.common.errors import ConflictError, NotFound, ValidationError
from app.core.activities.survey import finish_survey, start_survey, submit_answer
from app.core.types.dto import (
    ActivityRef,
    AttemptRef,
    ProductRecommendationRef,
    RewardIssued,
    SurveyAnswerRef,
    SurveyOptionRef,
    SurveyQuestionRef,
)
from app.core.types.enums import AttemptStatus, RewardType
from app.core.types.ids import (
    ActivityId,
    AttemptId,
    BrandId,
    ChannelAccountId,
    ProductRecommendationId,
    RewardId,
    SurveyQuestionId,
    UserId,
)

NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
USER = UserId(1)
CHANNEL = ChannelAccountId(1)
ACTIVITY_ID = ActivityId(1)
ATTEMPT_ID = AttemptId(1)


def _activity(kind: str = "survey", limits: dict[str, Any] | None = None) -> ActivityRef:
    return ActivityRef(
        id=ACTIVITY_ID,
        brand_id=BrandId(1),
        slug="demo_survey",
        display_name="Demo",
        kind=kind,  # type: ignore[arg-type]
        limits=limits if limits is not None else {"promocode_template": "DEMO-{random8}"},
    )


def _question(
    qid: int, position: int, *keys: str, qtype: str = "single_choice"
) -> SurveyQuestionRef:
    return SurveyQuestionRef(
        id=SurveyQuestionId(qid),
        activity_id=ACTIVITY_ID,
        position=position,
        text=f"q{qid}",
        question_type=qtype,  # type: ignore[arg-type]
        options=tuple(SurveyOptionRef(key=k, label=k, profile={f"attr{qid}": k}) for k in keys),
    )


def _card(cid: int, condition: dict[str, str], position: int = 1) -> ProductRecommendationRef:
    return ProductRecommendationRef(
        id=ProductRecommendationId(cid),
        activity_id=ACTIVITY_ID,
        condition=condition,
        product_name=f"card{cid}",
        caption=None,
        product_url=f"https://example.com/{cid}",
        position=position,
    )


def _attempt(status: AttemptStatus = "in_progress") -> AttemptRef:
    return AttemptRef(
        id=ATTEMPT_ID,
        user_id=USER,
        activity_id=ACTIVITY_ID,
        activity_slug="demo_survey",
        channel_account_id=CHANNEL,
        status=status,
        started_at=NOW,
        attempt_date=date(2026, 10, 12),
    )


class World:
    """Все фейковые репозитории с общим журналом вызовов."""

    def __init__(
        self,
        *,
        consent: bool = True,
        activity: ActivityRef | None = None,
        questions: Sequence[SurveyQuestionRef] = (),
        cards: Sequence[ProductRecommendationRef] = (),
        attempt: AttemptRef | None = None,
        answered: Sequence[SurveyAnswerRef] = (),
    ) -> None:
        self.calls: list[str] = []
        self.consent = consent
        self.activity = activity if activity is not None else _activity()
        self.questions = list(questions)
        self.cards = list(cards)
        self.attempt = attempt
        self.answers: list[SurveyAnswerRef] = list(answered)
        self.rewards: list[RewardIssued] = []
        self.consents = _Consents(self)
        self.catalog = _Catalog(self)
        self.attempts = _Attempts(self)
        self.surveys = _Answers(self)
        self.reward_repo = _Rewards(self)


class _Consents:
    def __init__(self, world: World) -> None:
        self._w = world

    async def has_active(self, user_id: UserId, policy_version: str) -> bool:
        self._w.calls.append("consents.has_active")
        return self._w.consent


class _Catalog:
    def __init__(self, world: World) -> None:
        self._w = world

    async def find_activity_by_slug(self, slug: str) -> ActivityRef | None:
        self._w.calls.append("catalog.find_activity_by_slug")
        return self._w.activity

    async def find_activity_by_id(self, activity_id: ActivityId) -> ActivityRef | None:
        self._w.calls.append("catalog.find_activity_by_id")
        return self._w.activity

    async def list_survey_questions(self, activity_id: ActivityId) -> list[SurveyQuestionRef]:
        self._w.calls.append("catalog.list_survey_questions")
        return list(self._w.questions)

    async def list_recommendations(self, activity_id: ActivityId) -> list[ProductRecommendationRef]:
        self._w.calls.append("catalog.list_recommendations")
        return list(self._w.cards)


class _Attempts:
    def __init__(self, world: World) -> None:
        self._w = world

    async def insert_in_progress(self, **kwargs: Any) -> AttemptRef | None:
        self._w.calls.append("attempts.insert_in_progress")
        self._w.attempt = _attempt()
        return self._w.attempt

    async def get(self, attempt_id: AttemptId) -> AttemptRef | None:
        self._w.calls.append("attempts.get")
        return self._w.attempt

    async def close_in_progress(
        self,
        attempt_id: AttemptId,
        status: AttemptStatus,
        summary: str | None,
        finished_at: datetime,
    ) -> AttemptRef | None:
        self._w.calls.append("attempts.close_in_progress")
        if self._w.attempt is None or self._w.attempt.status != "in_progress":
            return None
        self._w.attempt = replace(self._w.attempt, status=status, summary=summary)
        return self._w.attempt


class _Answers:
    def __init__(self, world: World) -> None:
        self._w = world

    async def insert(
        self,
        attempt_id: AttemptId,
        question_id: SurveyQuestionId,
        selected_option_keys: Sequence[str],
        free_text: str | None,
        answered_at: datetime,
    ) -> SurveyAnswerRef | None:
        self._w.calls.append("answers.insert")
        if any(a.question_id == question_id for a in self._w.answers):
            return None
        answer = SurveyAnswerRef(
            attempt_id=attempt_id,
            question_id=question_id,
            selected_option_keys=tuple(selected_option_keys),
            free_text=free_text,
            answered_at=answered_at,
        )
        self._w.answers.append(answer)
        return answer

    async def list_for_attempt(self, attempt_id: AttemptId) -> list[SurveyAnswerRef]:
        self._w.calls.append("answers.list_for_attempt")
        return list(self._w.answers)


class _Rewards:
    def __init__(self, world: World) -> None:
        self._w = world

    async def insert(
        self,
        attempt_id: AttemptId,
        reward_type: RewardType,
        code: str | None,
        product_recommendation_ids: Sequence[int] | None,
        wheel_prize_id: int | None,
        title: str | None,
        issued_at: datetime,
    ) -> RewardIssued | None:
        self._w.calls.append("rewards.insert")
        if any(r.reward_type == reward_type for r in self._w.rewards):
            return None
        reward = RewardIssued(
            id=RewardId(len(self._w.rewards) + 1),
            attempt_id=attempt_id,
            reward_type=reward_type,
            code=code,
            title=title,
            issued_at=issued_at,
            product_recommendation_ids=(
                tuple(product_recommendation_ids) if product_recommendation_ids else None
            ),
        )
        self._w.rewards.append(reward)
        return reward

    async def exists(self, attempt_id: AttemptId, reward_type: RewardType) -> bool:
        return any(r.reward_type == reward_type for r in self._w.rewards)


def _answered(world_questions: Sequence[SurveyQuestionRef]) -> list[SurveyAnswerRef]:
    return [
        SurveyAnswerRef(
            attempt_id=ATTEMPT_ID,
            question_id=q.id,
            selected_option_keys=(q.options[0].key,),
            free_text=None,
            answered_at=NOW,
        )
        for q in world_questions
    ]


async def _start(world: World) -> Any:
    return await start_survey(
        world.consents, world.attempts, world.catalog, USER, CHANNEL, "demo_survey", NOW
    )


async def _answer(world: World, qid: int, keys: Sequence[str], free_text: str | None = None) -> Any:
    return await submit_answer(
        world.consents,
        world.attempts,
        world.catalog,
        world.surveys,
        ATTEMPT_ID,
        SurveyQuestionId(qid),
        keys,
        free_text,
        NOW,
    )


async def _finish(world: World) -> Any:
    return await finish_survey(
        world.consents,
        world.attempts,
        world.catalog,
        world.surveys,
        world.reward_repo,
        ATTEMPT_ID,
        NOW,
        lambda n: "A" * n,
    )


# ----------------------------------------------------------------- правило 1


async def test_start_survey_asks_consent_first_and_touches_nothing_else_without_it() -> None:
    world = World(consent=False, questions=[_question(1, 1, "a")])

    with pytest.raises(ConflictError) as exc:
        await _start(world)

    assert exc.value.code == "consent_required"
    assert world.calls == ["consents.has_active"]


async def test_start_survey_consent_check_precedes_every_other_call() -> None:
    world = World(questions=[_question(1, 1, "a"), _question(2, 2, "b")])

    result = await _start(world)

    assert world.calls[0] == "consents.has_active"
    assert world.calls.index("consents.has_active") < world.calls.index(
        "attempts.insert_in_progress"
    )
    assert result.next_question is not None
    assert result.next_question.id == 1
    assert result.total_questions == 2
    assert result.kind == "survey"


async def test_start_survey_rejects_other_kind_before_inserting_attempt() -> None:
    world = World(activity=_activity("booking"), questions=[_question(1, 1, "a")])

    with pytest.raises(ValidationError) as exc:
        await _start(world)

    assert exc.value.code == "activity_kind_mismatch"
    assert "attempts.insert_in_progress" not in world.calls


async def test_start_survey_without_questions_is_not_found() -> None:
    world = World(questions=[])

    with pytest.raises(NotFound) as exc:
        await _start(world)

    assert exc.value.code == "survey_questions_not_found"


# ------------------------------------------------------------ submit_answer


@pytest.mark.parametrize(
    ("keys", "free_text"),
    [
        ([], None),  # пустой ответ
        (["zzz"], None),  # неизвестный вариант
        (["a", "b"], None),  # два варианта на single_choice
        (["a", "a"], None),  # повтор варианта
        (["a"], "текст"),  # свободный текст к вопросу с вариантами
    ],
)
async def test_submit_answer_rejects_invalid_choice_answer(
    keys: list[str], free_text: str | None
) -> None:
    world = World(questions=[_question(1, 1, "a", "b")], attempt=_attempt())

    with pytest.raises(ValidationError) as exc:
        await _answer(world, 1, keys, free_text)

    assert exc.value.code == "answer_invalid"
    assert "answers.insert" not in world.calls


@pytest.mark.parametrize(
    ("keys", "free_text"),
    [([], None), ([], "   "), (["a"], "текст"), ([], "x" * 1001)],
)
async def test_submit_answer_rejects_invalid_free_text_answer(
    keys: list[str], free_text: str | None
) -> None:
    world = World(questions=[_question(1, 1, qtype="free_text")], attempt=_attempt())

    with pytest.raises(ValidationError):
        await _answer(world, 1, keys, free_text)


async def test_submit_answer_accepts_trimmed_free_text() -> None:
    world = World(questions=[_question(1, 1, qtype="free_text")], attempt=_attempt())

    result = await _answer(world, 1, [], "  привет  ")

    assert world.answers[0].free_text == "привет"
    assert world.answers[0].selected_option_keys == ()
    assert result.is_last is True


async def test_submit_answer_multi_choice_accepts_several_distinct_options() -> None:
    world = World(
        questions=[_question(1, 1, "a", "b", "c", qtype="multi_choice")], attempt=_attempt()
    )

    await _answer(world, 1, ["a", "c"])

    assert world.answers[0].selected_option_keys == ("a", "c")


async def test_submit_answer_unknown_question_is_not_found() -> None:
    world = World(questions=[_question(1, 1, "a")], attempt=_attempt())

    with pytest.raises(NotFound) as exc:
        await _answer(world, 42, ["a"])

    assert exc.value.code == "question_not_found"


async def test_submit_answer_unknown_attempt_is_not_found() -> None:
    world = World(questions=[_question(1, 1, "a")], attempt=None)

    with pytest.raises(NotFound) as exc:
        await _answer(world, 1, ["a"])

    assert exc.value.code == "attempt_not_found"


async def test_submit_answer_on_closed_attempt_is_conflict() -> None:
    world = World(questions=[_question(1, 1, "a")], attempt=_attempt("completed"))

    with pytest.raises(ConflictError) as exc:
        await _answer(world, 1, ["a"])

    assert exc.value.code == "already_closed"


async def test_submit_answer_twice_for_one_question_is_conflict() -> None:
    world = World(questions=[_question(1, 1, "a"), _question(2, 2, "b")], attempt=_attempt())
    await _answer(world, 1, ["a"])

    with pytest.raises(ConflictError) as exc:
        await _answer(world, 1, ["a"])

    assert exc.value.code == "already_answered"
    assert len(world.answers) == 1


async def test_submit_answer_reports_progress_and_next_unanswered_question() -> None:
    questions = [_question(1, 1, "a"), _question(2, 2, "b"), _question(3, 3, "c")]
    world = World(questions=questions, attempt=_attempt())

    first = await _answer(world, 1, ["a"])
    # Ответ «не по порядку»: следующим остаётся первый неотвеченный, а не «следующий по номеру».
    third = await _answer(world, 3, ["c"])
    last = await _answer(world, 2, ["b"])

    assert (first.answered_count, first.total_questions, first.is_last) == (1, 3, False)
    assert first.next_question is not None and first.next_question.id == 2
    assert third.next_question is not None and third.next_question.id == 2
    assert (last.answered_count, last.is_last, last.next_question) == (3, True, None)


# ------------------------------------------------------------- finish_survey


def _finished_world(
    limits: dict[str, Any] | None = None,
    cards: Sequence[ProductRecommendationRef] | None = None,
) -> World:
    questions = [_question(1, 1, "a"), _question(2, 2, "b")]
    return World(
        activity=_activity(limits=limits),
        questions=questions,
        cards=[_card(1, {"attr1": "a"}), _card(2, {"attr2": "b"}, 2), _card(3, {}, 3)]
        if cards is None
        else cards,
        attempt=_attempt(),
        answered=_answered(questions),
    )


async def test_finish_survey_closes_attempt_before_granting_rewards() -> None:
    world = _finished_world()

    result = await _finish(world)

    assert world.calls.index("attempts.close_in_progress") < world.calls.index("rewards.insert")
    assert [r.reward_type for r in result.rewards] == ["recommendation_set", "promocode"]
    assert result.promocode == "DEMO-AAAAAAAA"
    assert result.summary == "attr1=a; attr2=b"
    assert dict(result.profile) == {"attr1": "a", "attr2": "b"}
    assert [c.id for c in result.recommendations] == [1, 2, 3]
    assert world.attempt is not None and world.attempt.status == "completed"
    assert world.rewards[0].product_recommendation_ids == (1, 2, 3)


async def test_finish_survey_without_template_grants_only_recommendations() -> None:
    world = _finished_world(limits={})

    result = await _finish(world)

    assert [r.reward_type for r in result.rewards] == ["recommendation_set"]
    assert result.promocode is None


async def test_finish_survey_without_cards_grants_only_promocode() -> None:
    world = _finished_world(cards=[])

    result = await _finish(world)

    assert [r.reward_type for r in result.rewards] == ["promocode"]
    assert result.recommendations == ()


async def test_finish_survey_with_nothing_to_grant_still_closes_attempt() -> None:
    world = _finished_world(limits={}, cards=[])

    result = await _finish(world)

    assert result.rewards == ()
    assert "rewards.insert" not in world.calls
    assert world.attempt is not None and world.attempt.status == "completed"


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(2, [1, 2]), (1, [1]), (0, [1, 2, 3]), ("2", [1, 2, 3]), (True, [1, 2, 3])],
)
async def test_finish_survey_applies_only_valid_max_recommendations(
    limit: Any, expected: list[int]
) -> None:
    world = _finished_world(limits={"max_recommendations": limit})

    result = await _finish(world)

    assert [c.id for c in result.recommendations] == expected


async def test_finish_survey_incomplete_does_not_close_attempt() -> None:
    questions = [_question(1, 1, "a"), _question(2, 2, "b")]
    world = World(questions=questions, attempt=_attempt(), answered=_answered(questions[:1]))

    with pytest.raises(ConflictError) as exc:
        await _finish(world)

    assert exc.value.code == "survey_incomplete"
    assert "attempts.close_in_progress" not in world.calls
    assert world.attempt is not None and world.attempt.status == "in_progress"


async def test_finish_survey_on_closed_attempt_is_conflict() -> None:
    world = _finished_world()
    world.attempt = _attempt("completed")

    with pytest.raises(ConflictError) as exc:
        await _finish(world)

    assert exc.value.code == "already_closed"
    assert "rewards.insert" not in world.calls


async def test_finish_survey_rejects_other_activity_kind() -> None:
    world = _finished_world()
    world.activity = _activity("booking")

    with pytest.raises(ValidationError) as exc:
        await _finish(world)

    assert exc.value.code == "activity_kind_mismatch"
    assert "attempts.close_in_progress" not in world.calls
