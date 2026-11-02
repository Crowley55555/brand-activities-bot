from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.types.dto import SurveyAnswerRef
from app.core.types.ids import AttemptId, SurveyQuestionId


@runtime_checkable
class SurveyAnswerRepo(Protocol):
    async def insert(
        self,
        attempt_id: AttemptId,
        question_id: SurveyQuestionId,
        selected_option_keys: Sequence[str],
        free_text: str | None,
        answered_at: datetime,
    ) -> SurveyAnswerRef | None:
        """`INSERT ... ON CONFLICT DO NOTHING RETURNING`.

        None - на этот вопрос в этом прохождении уже есть ответ
        (UNIQUE survey_answers_attempt_question): повторный клик не дублирует ответ.
        """
        ...

    async def list_for_attempt(self, attempt_id: AttemptId) -> list[SurveyAnswerRef]:
        """Ответы прохождения в порядке ответа."""
        ...
