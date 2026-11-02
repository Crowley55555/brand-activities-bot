from __future__ import annotations

from app.core.catalog.repository import CatalogRepo
from app.core.types.dto import SurveyQuestionRef
from app.core.types.ids import ActivityId


async def get_survey_questions(
    repo: CatalogRepo, activity_id: ActivityId
) -> list[SurveyQuestionRef]:
    """Вопросы опроса по порядку (`position`, затем id)."""
    questions = await repo.list_survey_questions(activity_id)
    return sorted(questions, key=lambda q: (q.position, q.id))
