from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types.dto import SurveyAnswerRef
from app.core.types.ids import AttemptId, SurveyQuestionId

_COLUMNS = "attempt_id, question_id, selected_option_keys, free_text, answered_at"


def _to_ref(row: Any) -> SurveyAnswerRef:
    keys = row.selected_option_keys
    return SurveyAnswerRef(
        attempt_id=AttemptId(row.attempt_id),
        question_id=SurveyQuestionId(row.question_id),
        selected_option_keys=tuple(str(k) for k in keys) if keys is not None else (),
        free_text=row.free_text,
        answered_at=row.answered_at,
    )


class SqlSurveyAnswerRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(
        self,
        attempt_id: AttemptId,
        question_id: SurveyQuestionId,
        selected_option_keys: Sequence[str],
        free_text: str | None,
        answered_at: datetime,
    ) -> SurveyAnswerRef | None:
        row = (
            await self._session.execute(
                text(
                    "INSERT INTO survey_answers "
                    "(attempt_id, question_id, selected_option_keys, free_text, "
                    " answered_at, created_at, updated_at) "
                    "VALUES (:aid, :qid, CAST(:keys AS jsonb), :free, :ts, :ts, :ts) "
                    "ON CONFLICT (attempt_id, question_id) DO NOTHING "
                    f"RETURNING {_COLUMNS}"
                ),
                {
                    "aid": int(attempt_id),
                    "qid": int(question_id),
                    # Для free_text вариантов нет: храним NULL, а не пустой список.
                    "keys": json.dumps(list(selected_option_keys))
                    if selected_option_keys
                    else None,
                    "free": free_text,
                    "ts": answered_at,
                },
            )
        ).one_or_none()
        return _to_ref(row) if row is not None else None

    async def list_for_attempt(self, attempt_id: AttemptId) -> list[SurveyAnswerRef]:
        rows = (
            await self._session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM survey_answers "
                    "WHERE attempt_id = :aid ORDER BY answered_at, id"
                ),
                {"aid": int(attempt_id)},
            )
        ).all()
        return [_to_ref(row) for row in rows]
