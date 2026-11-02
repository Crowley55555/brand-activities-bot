"""Правила опроса: ответы -> профиль гостя (table-driven, ADR-12).

Здесь нет ни одного названия бренда, вопроса, атрибута или продукта. Таблица
«вариант ответа -> атрибут профиля» хранится в данных: `survey_questions.options[].profile`
(сид-миграция активности). Движок только складывает вклады выбранных вариантов.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.core.types.dto import SurveyAnswerRef, SurveyQuestionRef


def compute_condition(
    questions: Sequence[SurveyQuestionRef], answers: Sequence[SurveyAnswerRef]
) -> dict[str, str]:
    """Профиль гостя: объединение `profile` выбранных вариантов.

    Вопросы обходятся по порядку (`position`, id); если два ответа задают один атрибут,
    побеждает более поздний вопрос. Ответы на вопросы, которых нет в списке (например,
    удалённые), и неизвестные ключи вариантов пропускаются. Свободный текст и варианты
    без `profile` в профиль ничего не добавляют.
    """
    answers_by_question = {answer.question_id: answer for answer in answers}
    profile: dict[str, str] = {}
    for question in sorted(questions, key=lambda q: (q.position, q.id)):
        answer = answers_by_question.get(question.id)
        if answer is None:
            continue
        options = {option.key: option for option in question.options}
        for key in answer.selected_option_keys:
            option = options.get(key)
            if option is not None:
                profile.update(option.profile)
    return profile


def summarize_profile(profile: Mapping[str, str]) -> str | None:
    """Строка для `attempts.summary`: `атрибут=значение; ...` по алфавиту. Без ПДн.

    Пустой профиль -> None (нечего писать).
    """
    if not profile:
        return None
    return "; ".join(f"{attribute}={profile[attribute]}" for attribute in sorted(profile))
