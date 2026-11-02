"""seed brand_a survey

Revision ID: 0003_seed_brand_a_survey
Revises: 0002_seed_brands
Create Date: 2026-10-15

Активность Бренд A «Код кожи» (опрос из пяти вопросов). Правило 5: slug,
названия, тексты вопросов, варианты ответов и настройки награды живут только
здесь (и в 0004 для карточек продуктов), не в коде.

Формат данных, на который опирается универсальный движок
`core.activities.survey`:

* `activities.limits` - настройки активности:
    - `max_per_day` / `requires_moderation` - ограничения (02 §5);
    - `promocode_template` - шаблон одноразового промокода, `{randomN}` = N
      символов [A-Z0-9] (ADR-13). Нет ключа - промокод не выдаётся;
    - `max_recommendations` - сколько карточек показать гостю (по убыванию
      специфичности условия, затем по `position`).
* `survey_questions.options[]` - `{"key", "label", "profile"}`. `profile` -
  вклад варианта в профиль гостя (атрибут -> значение). Профиль - объединение
  `profile` выбранных вариантов; карточка `product_recommendations` подходит,
  если её `condition` целиком входит в профиль (ADR-12, таблица
  «ответ -> атрибут» хранится в данных, а не в `rules.py`).

Миграция идемпотентная: активность - `ON CONFLICT (slug) DO NOTHING`, вопросы -
вставка только если на этой позиции у активности ещё нет вопроса.
`downgrade` удаляет только то, что создал `upgrade`; если по активности уже есть
прохождения, FK RESTRICT остановит откат (на проде downgrade не запускаем).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0003_seed_brand_a_survey"
down_revision: str | None = "0002_seed_brands"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_BRAND_SLUG = "brand_a"
_ACTIVITY_SLUG = "brand_a_skin_survey"
_ACTIVITY_NAME = "Код кожи"

_LIMITS: dict[str, Any] = {
    "max_per_day": 1,
    "requires_moderation": False,
    "promocode_template": "BRAND_A-{random8}",
    "max_recommendations": 3,
}


def _option(key: str, label: str, attribute: str, value: str) -> dict[str, Any]:
    return {"key": key, "label": label, "profile": {attribute: value}}


# (position, text, options). Все вопросы - single_choice.
_QUESTIONS: list[tuple[int, str, list[dict[str, Any]]]] = [
    (
        1,
        "Какой у вас тип кожи?",
        [
            _option("combo", "Комбинированная", "skin_type", "combo"),
            _option("dry", "Сухая", "skin_type", "dry"),
            _option("oily", "Жирная", "skin_type", "oily"),
            _option("sensitive", "Чувствительная", "skin_type", "sensitive"),
        ],
    ),
    (
        2,
        "Сколько вам лет?",
        [
            _option("age_18_25", "18-25", "age", "18_25"),
            _option("age_26_35", "26-35", "age", "26_35"),
            _option("age_36_plus", "36 и старше", "age", "36_plus"),
        ],
    ),
    (
        3,
        "Кожа часто реагирует покраснением или раздражением?",
        [
            _option("yes", "Да", "sensitivity", "yes"),
            _option("no", "Нет", "sensitivity", "no"),
        ],
    ),
    (
        4,
        "Блестит ли Т-зона (лоб, нос, подбородок) к середине дня?",
        [
            _option("yes", "Да", "oily_tzone", "yes"),
            _option("no", "Нет", "oily_tzone", "no"),
        ],
    ),
    (
        5,
        "Есть ли пигментные пятна или неровный тон?",
        [
            _option("yes", "Да", "pigmentation", "yes"),
            _option("no", "Нет", "pigmentation", "no"),
        ],
    ),
]


_INSERT_ACTIVITY = sa.text(
    """
    INSERT INTO activities (brand_id, slug, display_name, kind, is_active, limits,
                            created_at, updated_at)
    SELECT b.id, :slug, :display_name, 'survey', TRUE, CAST(:limits AS jsonb),
           now(), now()
    FROM brands b
    WHERE b.slug = :brand_slug
    ON CONFLICT (slug) DO NOTHING
    """
)

_INSERT_QUESTION = sa.text(
    """
    INSERT INTO survey_questions (activity_id, position, text, question_type,
                                  options, created_at, updated_at)
    SELECT a.id, :position, :text, 'single_choice', CAST(:options AS jsonb),
           now(), now()
    FROM activities a
    WHERE a.slug = :slug
      AND NOT EXISTS (
          SELECT 1 FROM survey_questions q
          WHERE q.activity_id = a.id
            AND q.position = :position
            AND q.deleted_at IS NULL
      )
    """
)

_DELETE_QUESTIONS = sa.text(
    """
    DELETE FROM survey_questions
    WHERE activity_id IN (SELECT id FROM activities WHERE slug = :slug)
    """
)

_DELETE_ACTIVITY = sa.text("DELETE FROM activities WHERE slug = :slug")


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        _INSERT_ACTIVITY,
        {
            "slug": _ACTIVITY_SLUG,
            "display_name": _ACTIVITY_NAME,
            "limits": json.dumps(_LIMITS, ensure_ascii=False),
            "brand_slug": _BRAND_SLUG,
        },
    )
    for position, question_text, options in _QUESTIONS:
        bind.execute(
            _INSERT_QUESTION,
            {
                "slug": _ACTIVITY_SLUG,
                "position": position,
                "text": question_text,
                "options": json.dumps(options, ensure_ascii=False),
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(_DELETE_QUESTIONS, {"slug": _ACTIVITY_SLUG})
    bind.execute(_DELETE_ACTIVITY, {"slug": _ACTIVITY_SLUG})
