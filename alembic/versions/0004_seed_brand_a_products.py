"""seed brand_a products

Revision ID: 0004_seed_brand_a_products
Revises: 0003_seed_brand_a_survey
Create Date: 2026-10-15

Карточки рекомендованных продуктов Бренд A для опроса «Код кожи». Правило 5:
названия, подписи и ссылки живут только здесь. Тексты и URL - плейсхолдеры
(example.com), реальные значения заказчика подставляются отдельной миграцией
или правкой в админке (ADR-14: выданные награды хранят только id карточек, так
что правка текста/URL сразу видна и в истории гостя).

`condition` - простое правило: карточка подходит, если все её пары
«атрибут -> значение» есть в профиле гостя (см. 0003). Пустое условие `{}` -
карточка для любого гостя. Выдача: по убыванию числа условий (специфичнее -
выше), затем по `position`, не больше `limits.max_recommendations`.

Миграция идемпотентная: карточка вставляется, только если у активности ещё нет
карточки с таким `product_name`. `downgrade` удаляет карточки активности.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0004_seed_brand_a_products"
down_revision: str | None = "0003_seed_brand_a_survey"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_ACTIVITY_SLUG = "brand_a_skin_survey"

# (position, condition, product_name, caption, product_url)
_CARDS: list[tuple[int, dict[str, Any], str, str, str]] = [
    (
        1,
        {"skin_type": "combo"},
        "Бренд A Крем для комбинированной кожи",
        "Балансирует сухие и жирные зоны, лёгкая текстура",
        "https://example.com/brand_a/combo-cream",
    ),
    (
        2,
        {"skin_type": "dry"},
        "Бренд A Питательный крем для сухой кожи",
        "Интенсивно увлажняет и восстанавливает барьер кожи",
        "https://example.com/brand_a/dry-cream",
    ),
    (
        3,
        {"skin_type": "oily"},
        "Бренд A Матирующий гель для жирной кожи",
        "Контролирует блеск и сужает поры",
        "https://example.com/brand_a/oily-gel",
    ),
    (
        4,
        {"skin_type": "sensitive"},
        "Бренд A Успокаивающий крем для чувствительной кожи",
        "Снимает покраснения, без отдушек",
        "https://example.com/brand_a/sensitive-cream",
    ),
    (
        5,
        {"skin_type": "dry", "age": "36_plus"},
        "Бренд A Антивозрастной крем для зрелой сухой кожи",
        "Укрепляет упругость и питает глубокие слои",
        "https://example.com/brand_a/dry-age-cream",
    ),
    (
        6,
        {"pigmentation": "yes"},
        "Бренд A Сыворотка против пигментации",
        "Выравнивает тон и осветляет пятна",
        "https://example.com/brand_a/pigment-serum",
    ),
    (
        7,
        {"sensitivity": "yes"},
        "Бренд A Мягкий гель для умывания",
        "Деликатное очищение без сульфатов",
        "https://example.com/brand_a/gentle-cleanser",
    ),
    (
        8,
        {"oily_tzone": "yes"},
        "Бренд A Матирующий тоник для Т-зоны",
        "Убирает блеск в Т-зоне в течение дня",
        "https://example.com/brand_a/tzone-toner",
    ),
    (
        9,
        {},
        "Бренд A Флюид SPF 30",
        "Ежедневная защита от солнца для любого типа кожи",
        "https://example.com/brand_a/spf-fluid",
    ),
]


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


def upgrade() -> None:
    bind = op.get_bind()
    for position, condition, product_name, caption, product_url in _CARDS:
        bind.execute(
            _INSERT_CARD,
            {
                "slug": _ACTIVITY_SLUG,
                "position": position,
                "condition": json.dumps(condition, ensure_ascii=False),
                "product_name": product_name,
                "caption": caption,
                "product_url": product_url,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(_DELETE_CARDS, {"slug": _ACTIVITY_SLUG})
