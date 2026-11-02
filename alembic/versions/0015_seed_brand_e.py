"""seed brand_e

Revision ID: 0015_seed_brand_e
Revises: 0014_seed_brand_d
Create Date: 2026-10-21

Бренд E «Код настроения» (фаза 6, ADR-44; решения B1 и B2 ревью фазы 6). Только данные, кода нет:
опрос и фото переиспользуют `core.activities.survey` и `core.activities.ugc`. Правило 5: slug'и,
вопросы, варианты, ароматы и лимиты живут только здесь.

Две активности бренда `brand_e`:
* `brand_e_scent_match` (kind=survey) - подбор аромата. Пять вопросов: настроение, впечатление,
  ноты (multi_choice), время суток, сезон. Каждый вариант несёт `profile` (ADR-12). Ноты - bool-
  атрибуты `note_<x>: "yes"` (B2): у каждой ноты свой атрибут, поэтому выбор нескольких нот не
  перетирает предыдущий. Карточки: 6 специфичных (2-5 условий) и 4 «по настроению» (U3: fallback,
  есть всегда, так как настроение обязательно). Лимит 3 карточки (U2).
* `brand_e_photo` (kind=ugc) - фото аромата за промокод. Модерация есть, ваучера нет (B1):
  `voucher_title` = null, `voucher_code_template` не задан. approve выдаёт только промокод.

Промокоды обеих активностей: BRAND_E-{random8}.

`downgrade` удаляет то, что создал `upgrade`. Если по активности есть прохождения, FK RESTRICT
остановит откат (на проде downgrade не запускаем, как 0003/0004).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0015_seed_brand_e"
down_revision: str | None = "0014_seed_brand_d"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_BRAND_SLUG = "brand_e"
_SURVEY_SLUG = "brand_e_scent_match"
_PHOTO_SLUG = "brand_e_photo"
_PROMO_TEMPLATE = "BRAND_E-{random8}"
_PRODUCT_URL_PREFIX = "https://example.com/brand_e/"

# (slug, display_name, kind, limits)
_ACTIVITIES: list[tuple[str, str, str, dict[str, Any]]] = [
    (
        _SURVEY_SLUG,
        "Код настроения",
        "survey",
        {
            "max_per_day": 1,
            "requires_moderation": False,
            "promocode_template": _PROMO_TEMPLATE,
            "max_recommendations": 3,
        },
    ),
    (
        _PHOTO_SLUG,
        "Фото аромата",
        "ugc",
        {
            "max_per_day": 1,
            "requires_moderation": True,
            "max_file_bytes": 5242880,
            "allowed_mimes": ["image/jpeg", "image/png", "image/webp"],
            "max_resubmits": 1,
            "voucher_title": None,
            "promocode_template": _PROMO_TEMPLATE,
            "reject_reasons": {
                "low_quality": "Низкое качество фото",
                "no_perfume": "Аромат не виден",
                "off_topic": "Фото не по теме",
                "other": "Другое",
            },
            "system_reject_reasons": {
                "mime_mismatch": "Формат файла не поддерживается",
                "file_not_found": "Файл не удалось получить от Telegram",
                "file_too_large": "Файл слишком большой",
                "ingest_dead": "Техническая ошибка",
                "consent_revoked": "Согласие отозвано",
            },
        },
    ),
]


def _option(key: str, label: str, attribute: str, value: str) -> dict[str, Any]:
    return {"key": key, "label": label, "profile": {attribute: value}}


def _note(key: str, label: str) -> dict[str, Any]:
    """Нота (multi_choice): bool-атрибут `note_<key>: "yes"` (решение B2)."""
    return {"key": key, "label": label, "profile": {f"note_{key}": "yes"}}


# (position, text, question_type, options)
_QUESTIONS: list[tuple[int, str, str, list[dict[str, Any]]]] = [
    (
        1,
        "Какое у вас сейчас настроение?",
        "single_choice",
        [
            _option("calm", "Спокойное", "mood", "calm"),
            _option("energetic", "Энергичное", "mood", "energetic"),
            _option("romantic", "Романтичное", "mood", "romantic"),
            _option("confident", "Уверенное", "mood", "confident"),
        ],
    ),
    (
        2,
        "Какое впечатление хотите произвести?",
        "single_choice",
        [
            _option("fresh", "Свежее", "impression", "fresh"),
            _option("warm", "Тёплое", "impression", "warm"),
            _option("mysterious", "Таинственное", "impression", "mysterious"),
            _option("elegant", "Элегантное", "impression", "elegant"),
        ],
    ),
    (
        3,
        "Какие ноты вам ближе?",
        "multi_choice",
        [
            _note("citrus", "Цитрус"),
            _note("floral", "Цветочные"),
            _note("woody", "Древесные"),
            _note("oriental", "Восточные"),
            _note("gourmand", "Гурманские"),
        ],
    ),
    (
        4,
        "Время суток для аромата?",
        "single_choice",
        [
            _option("day", "День", "time", "day"),
            _option("evening", "Вечер", "time", "evening"),
            _option("anytime", "Любое", "time", "anytime"),
        ],
    ),
    (
        5,
        "Любимое время года?",
        "single_choice",
        [
            _option("spring", "Весна", "season", "spring"),
            _option("summer", "Лето", "season", "summer"),
            _option("autumn", "Осень", "season", "autumn"),
            _option("winter", "Зима", "season", "winter"),
        ],
    ),
]

# (position, condition, product_name, caption, url_slug). Позиции 1-6 - специфичные карточки
# (2-5 условий), 7-10 - «по настроению» (U3). Условия - скалярные пары (канон 03 §1).
_CARDS: list[tuple[int, dict[str, str], str, str, str]] = [
    (
        1,
        {
            "mood": "calm",
            "impression": "fresh",
            "note_citrus": "yes",
            "note_floral": "yes",
            "season": "spring",
        },
        "Бренд E Serenity",
        "Свежий цитрусово-цветочный аромат для спокойного дня",
        "serenity",
    ),
    (
        2,
        {"mood": "calm", "note_floral": "yes"},
        "Бренд E Lotus",
        "Мягкий цветочный аромат без резких акцентов",
        "lotus",
    ),
    (
        3,
        {"mood": "energetic", "note_citrus": "yes", "season": "summer"},
        "Бренд E Spark",
        "Яркий цитрусовый заряд на весь день",
        "spark",
    ),
    (
        4,
        {"mood": "romantic", "impression": "warm", "time": "evening", "season": "autumn"},
        "Бренд E Velvet",
        "Тёплый бархатистый аромат для вечернего свидания",
        "velvet",
    ),
    (
        5,
        {"mood": "confident", "impression": "mysterious"},
        "Бренд E Nocturne",
        "Таинственный аромат с глубоким шлейфом",
        "nocturne",
    ),
    (
        6,
        {"mood": "confident", "note_oriental": "yes", "note_woody": "yes"},
        "Бренд E Confidence",
        "Восточно-древесный аромат для уверенного образа",
        "confidence",
    ),
    (
        7,
        {"mood": "calm"},
        "Бренд E Calm Essence",
        "Универсальный выбор для спокойного настроения",
        "calm-essence",
    ),
    (
        8,
        {"mood": "energetic"},
        "Бренд E Pulse",
        "Универсальный выбор для энергичного настроения",
        "pulse",
    ),
    (
        9,
        {"mood": "romantic"},
        "Бренд E Rose Veil",
        "Универсальный выбор для романтичного настроения",
        "rose-veil",
    ),
    (
        10,
        {"mood": "confident"},
        "Бренд E Iconic",
        "Универсальный выбор для уверенного настроения",
        "iconic",
    ),
]


_INSERT_ACTIVITY = sa.text(
    """
    INSERT INTO activities (brand_id, slug, display_name, kind, is_active, limits,
                            created_at, updated_at)
    SELECT b.id, :slug, :display_name, :kind, TRUE, CAST(:limits AS jsonb), now(), now()
    FROM brands b
    WHERE b.slug = :brand_slug
    ON CONFLICT (slug) DO NOTHING
    """
)

_INSERT_QUESTION = sa.text(
    """
    INSERT INTO survey_questions (activity_id, position, text, question_type,
                                  options, created_at, updated_at)
    SELECT a.id, :position, :text, :question_type, CAST(:options AS jsonb), now(), now()
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

_INSERT_CARD = sa.text(
    """
    INSERT INTO product_recommendations (activity_id, condition, product_name, caption,
                                         product_url, position, is_active,
                                         created_at, updated_at)
    SELECT a.id, CAST(:condition AS jsonb), :product_name, :caption, :product_url,
           :position, TRUE, now(), now()
    FROM activities a
    WHERE a.slug = :slug
      AND NOT EXISTS (
          SELECT 1 FROM product_recommendations p
          WHERE p.activity_id = a.id
            AND p.product_name = :product_name
      )
    """
)

_DELETE_CARDS = sa.text(
    """
    DELETE FROM product_recommendations
    WHERE activity_id IN (SELECT id FROM activities WHERE slug = :slug)
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
    for slug, display_name, kind, limits in _ACTIVITIES:
        bind.execute(
            _INSERT_ACTIVITY,
            {
                "slug": slug,
                "display_name": display_name,
                "kind": kind,
                "limits": json.dumps(limits, ensure_ascii=False),
                "brand_slug": _BRAND_SLUG,
            },
        )
    for position, question_text, question_type, options in _QUESTIONS:
        bind.execute(
            _INSERT_QUESTION,
            {
                "slug": _SURVEY_SLUG,
                "position": position,
                "text": question_text,
                "question_type": question_type,
                "options": json.dumps(options, ensure_ascii=False),
            },
        )
    for position, condition, product_name, caption, url_slug in _CARDS:
        bind.execute(
            _INSERT_CARD,
            {
                "slug": _SURVEY_SLUG,
                "position": position,
                "condition": json.dumps(condition, ensure_ascii=False),
                "product_name": product_name,
                "caption": caption,
                "product_url": f"{_PRODUCT_URL_PREFIX}{url_slug}",
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    # Порядок важен: карточки и вопросы уходят раньше активности (FK).
    for slug in (_SURVEY_SLUG, _PHOTO_SLUG):
        bind.execute(_DELETE_CARDS, {"slug": slug})
        bind.execute(_DELETE_QUESTIONS, {"slug": slug})
        bind.execute(_DELETE_ACTIVITY, {"slug": slug})
