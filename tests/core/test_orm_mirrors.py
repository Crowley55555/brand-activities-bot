"""ORM-модели фазы 2 - зеркала схемы 0001; тест ловит дрейф (alembic target_metadata=None)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db.models import Base
from app.db.models.attempts import Attempt, SurveyAnswer, UgcSubmission
from app.db.models.catalog import Activity, ProductRecommendation, SurveyQuestion
from app.db.models.jobs import Job
from app.db.models.rewards import Reward

MODELS = [
    Activity,
    SurveyQuestion,
    ProductRecommendation,
    Attempt,
    SurveyAnswer,
    Reward,
    UgcSubmission,
    Job,
]


def _rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__tablename__)
def test_columns_and_nullability_match_database(model: type[Base], sync_engine: Engine) -> None:
    table = model.__table__  # type: ignore[attr-defined]
    database = {
        r.column_name: r.is_nullable == "YES"
        for r in _rows(
            sync_engine,
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t",
            t=table.name,
        )
    }

    assert {c.name: c.nullable for c in table.columns} == database


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__tablename__)
def test_declared_indexes_and_checks_exist_in_database(
    model: type[Base], sync_engine: Engine
) -> None:
    table = model.__table__  # type: ignore[attr-defined]
    indexes = {
        r.indexname
        for r in _rows(
            sync_engine, "SELECT indexname FROM pg_indexes WHERE tablename = :t", t=table.name
        )
    }
    constraints = {
        r.conname
        for r in _rows(
            sync_engine,
            "SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass)",
            t=table.name,
        )
    }

    assert {i.name for i in table.indexes} <= indexes
    assert {c.name for c in table.constraints if c.name} <= constraints


def test_survey_question_text_column_keeps_database_name() -> None:
    """Атрибут `question_text` не должен менять имя колонки `text` в БД."""
    assert SurveyQuestion.__table__.c["text"].name == "text"  # type: ignore[attr-defined]
    assert SurveyQuestion.question_text.property.columns[0].name == "text"
