"""Правила опроса и подбор карточек - чистая логика без БД (ADR-12)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from app.core.activities.survey.rules import compute_condition, summarize_profile
from app.core.catalog import (
    condition_matches,
    get_recommendations_by_condition,
    get_recommendations_by_ids,
    get_survey_questions,
)
from app.core.types.dto import (
    ProductRecommendationRef,
    SurveyAnswerRef,
    SurveyOptionRef,
    SurveyQuestionRef,
)
from app.core.types.enums import QuestionType
from app.core.types.ids import (
    ActivityId,
    AttemptId,
    ProductRecommendationId,
    SurveyQuestionId,
)

ACTIVITY = ActivityId(1)
NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)


def _question(
    qid: int,
    position: int,
    options: Sequence[tuple[str, dict[str, str]]],
    question_type: QuestionType = "single_choice",
) -> SurveyQuestionRef:
    return SurveyQuestionRef(
        id=SurveyQuestionId(qid),
        activity_id=ACTIVITY,
        position=position,
        text=f"q{qid}",
        question_type=question_type,
        options=tuple(SurveyOptionRef(key=k, label=k.upper(), profile=p) for k, p in options),
    )


def _answer(qid: int, *keys: str, free_text: str | None = None) -> SurveyAnswerRef:
    return SurveyAnswerRef(
        attempt_id=AttemptId(1),
        question_id=SurveyQuestionId(qid),
        selected_option_keys=tuple(keys),
        free_text=free_text,
        answered_at=NOW,
    )


def _card(cid: int, position: int, condition: dict[str, str]) -> ProductRecommendationRef:
    return ProductRecommendationRef(
        id=ProductRecommendationId(cid),
        activity_id=ACTIVITY,
        condition=condition,
        product_name=f"card{cid}",
        caption=None,
        product_url=f"https://example.com/{cid}",
        position=position,
    )


QUESTIONS = [
    _question(10, 1, [("a", {"color": "red"}), ("b", {"color": "blue"})]),
    _question(11, 2, [("x", {"size": "s"}), ("y", {"size": "l"}), ("z", {})]),
]


# ------------------------------------------------------- compute_condition


def test_compute_condition_merges_profiles_of_selected_options() -> None:
    profile = compute_condition(QUESTIONS, [_answer(10, "a"), _answer(11, "y")])

    assert profile == {"color": "red", "size": "l"}


def test_compute_condition_does_not_depend_on_answer_order() -> None:
    first = compute_condition(QUESTIONS, [_answer(10, "a"), _answer(11, "y")])
    second = compute_condition(QUESTIONS, [_answer(11, "y"), _answer(10, "a")])

    assert first == second


def test_compute_condition_later_question_wins_on_same_attribute() -> None:
    questions = [
        _question(1, 1, [("a", {"tone": "warm"})]),
        _question(2, 2, [("b", {"tone": "cold"})]),
    ]

    assert compute_condition(questions, [_answer(1, "a"), _answer(2, "b")]) == {"tone": "cold"}
    # Порядок определяется position вопроса, а не порядком ответов.
    assert compute_condition(questions, [_answer(2, "b"), _answer(1, "a")]) == {"tone": "cold"}


def test_compute_condition_skips_unanswered_unknown_and_profileless() -> None:
    answers = [
        _answer(10, "nope"),  # неизвестный ключ варианта
        _answer(11, "z"),  # вариант без вклада в профиль
        _answer(99, "a"),  # вопрос, которого нет (например, удалён)
    ]

    assert compute_condition(QUESTIONS, answers) == {}


def test_compute_condition_multi_choice_adds_every_selected_option() -> None:
    questions = [
        _question(1, 1, [("a", {"likes_a": "yes"}), ("b", {"likes_b": "yes"})], "multi_choice")
    ]

    assert compute_condition(questions, [_answer(1, "a", "b")]) == {
        "likes_a": "yes",
        "likes_b": "yes",
    }


def test_compute_condition_free_text_adds_nothing() -> None:
    questions = [_question(1, 1, [], "free_text")]

    assert compute_condition(questions, [_answer(1, free_text="что угодно")]) == {}


def test_summarize_profile_is_sorted_and_none_for_empty() -> None:
    assert summarize_profile({"size": "l", "color": "red"}) == "color=red; size=l"
    assert summarize_profile({}) is None


# ----------------------------------------------------------- подбор карточек


class FakeCatalogRepo:
    def __init__(
        self,
        cards: Sequence[ProductRecommendationRef] = (),
        questions: Sequence[SurveyQuestionRef] = (),
    ) -> None:
        self._cards = list(cards)
        self._questions = list(questions)
        self.by_ids_calls: list[Sequence[int]] = []

    async def list_recommendations(
        self, activity_id: ActivityId
    ) -> list[ProductRecommendationRef]:
        return list(self._cards)

    async def list_recommendations_by_ids(
        self, ids: Sequence[int]
    ) -> list[ProductRecommendationRef]:
        self.by_ids_calls.append(ids)
        return [card for card in self._cards if card.id in ids]

    async def list_survey_questions(self, activity_id: ActivityId) -> list[SurveyQuestionRef]:
        return list(self._questions)


@pytest.mark.parametrize(
    ("condition", "profile", "expected"),
    [
        ({}, {}, True),  # пустое условие подходит любому профилю
        ({}, {"a": "1"}, True),
        ({"a": "1"}, {"a": "1", "b": "2"}, True),
        ({"a": "1", "b": "2"}, {"a": "1", "b": "2"}, True),
        ({"a": "1", "b": "2"}, {"a": "1"}, False),  # не хватает атрибута
        ({"a": "1"}, {"a": "2"}, False),  # другое значение
        ({"a": "1"}, {}, False),
    ],
)
def test_condition_matches_is_containment(
    condition: dict[str, str], profile: dict[str, str], expected: bool
) -> None:
    assert condition_matches(condition, profile) is expected


async def test_recommendations_are_ordered_by_specificity_then_position() -> None:
    repo = FakeCatalogRepo(
        [
            _card(1, 1, {"a": "1"}),
            _card(2, 2, {"a": "1", "b": "2"}),  # специфичнее - выше, хотя position больше
            _card(3, 3, {"c": "3"}),
            _card(4, 9, {}),  # для всех - в конце
            _card(5, 4, {"z": "9"}),  # не подходит
        ]
    )

    result = await get_recommendations_by_condition(
        repo, ACTIVITY, {"a": "1", "b": "2", "c": "3"}
    )

    assert [card.id for card in result] == [2, 1, 3, 4]


async def test_recommendations_ties_are_broken_by_position_then_id() -> None:
    repo = FakeCatalogRepo(
        [_card(7, 2, {"a": "1"}), _card(5, 2, {"a": "1"}), _card(6, 1, {"a": "1"})]
    )

    result = await get_recommendations_by_condition(repo, ACTIVITY, {"a": "1"})

    assert [card.id for card in result] == [6, 5, 7]


async def test_recommendations_for_unmatched_profile_are_empty() -> None:
    repo = FakeCatalogRepo([_card(1, 1, {"a": "1"})])

    assert await get_recommendations_by_condition(repo, ACTIVITY, {"a": "2"}) == []


async def test_recommendations_by_ids_keep_requested_order_and_skip_unknown() -> None:
    repo = FakeCatalogRepo([_card(1, 1, {}), _card(2, 2, {}), _card(3, 3, {})])

    result = await get_recommendations_by_ids(repo, [3, 99, 1])

    assert [card.id for card in result] == [3, 1]


async def test_recommendations_by_empty_ids_do_not_hit_the_repo() -> None:
    repo = FakeCatalogRepo([_card(1, 1, {})])

    assert await get_recommendations_by_ids(repo, []) == []
    assert repo.by_ids_calls == []


async def test_survey_questions_are_returned_in_position_order() -> None:
    repo = FakeCatalogRepo(
        questions=[_question(3, 3, []), _question(1, 1, []), _question(2, 2, [])]
    )

    result = await get_survey_questions(repo, ACTIVITY)

    assert [q.id for q in result] == [1, 2, 3]
