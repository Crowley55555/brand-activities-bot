"""Сид Бренд D (фаза 5, ADR-43): 3 активности, призы колеса, limits рамки и подтверждения.

Правило 5: slug'и Бренд D и названия призов живут только в этой миграции. У wheel_prizes нет
slug (решение U7): призы идентифицируются id, вставка идемпотентна по (activity_id, title).
Limits: system_reject_reasons для сбоя рендера рамки (U4), promocode_template для подарка за пост.
Коды промокодов призов колеса - B3: code_template у призов prize_kind='promocode' (колонка из 0013).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0014_seed_brand_d"
down_revision: str | None = "0013_brand_d_schema"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_ACTIVITIES: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    (
        "brand_d_wheel",
        "Колесо момента",
        "wheel",
        {"max_per_day": 1, "wheel_prizes_mode": "random_weighted"},
    ),
    (
        "brand_d_polaroid",
        "Polaroid-фото",
        "photo_frame",
        {
            "max_per_day": 1,
            "frame_slug": "brand_c_polaroid",
            "max_file_bytes": 10485760,
            "allowed_mimes": ["image/jpeg", "image/png"],
            "system_reject_reasons": {"polaroid_render_failed": "Не удалось обработать фото"},
        },
    ),
    (
        "brand_d_confirmation",
        "Подарок за пост",
        "confirmation",
        {
            "max_per_day": 1,
            "requires_moderation": True,
            "reject_reasons": {
                "invalid_link": "Неверная ссылка",
                "not_posted": "Пост не опубликован",
                "other": "Другое",
            },
            "promocode_template": "BRAND_D-{random8}",
        },
    ),
)

# (title, prize_kind, weight, stock_left, code_template)
_PRIZES: tuple[tuple[str, str, int, int | None, str | None], ...] = (
    ("Пробник крема (JS Big)", "mini_gift", 1, 20, None),
    ("Пробник крема (JS Small)", "mini_gift", 3, 50, None),
    ("Комплимент стилиста", "compliment", 5, None, None),
    ("Промокод -10%", "promocode", 4, None, "BRAND_D10-{random8}"),
    ("Промокод -20%", "promocode", 2, 10, "BRAND_D20-{random8}"),
)

_INSERT_ACTIVITY = sa.text(
    """
    INSERT INTO activities (brand_id, slug, display_name, kind, is_active, limits,
                            created_at, updated_at)
    SELECT b.id, :slug, :display_name, :kind, TRUE, CAST(:limits AS jsonb), now(), now()
    FROM brands b
    WHERE b.slug = 'brand_d'
    ON CONFLICT (slug) DO NOTHING
    """
)
_INSERT_PRIZE = sa.text(
    """
    INSERT INTO wheel_prizes (activity_id, title, prize_kind, weight, stock_left, code_template,
                              created_at, updated_at)
    SELECT a.id, :title, :kind, :weight, :stock, :template, now(), now()
    FROM activities a
    WHERE a.slug = 'brand_d_wheel'
      AND NOT EXISTS (
          SELECT 1 FROM wheel_prizes p WHERE p.activity_id = a.id AND p.title = :title
      )
    """
)


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
            },
        )
    for title, prize_kind, weight, stock, template in _PRIZES:
        bind.execute(
            _INSERT_PRIZE,
            {
                "title": title,
                "kind": prize_kind,
                "weight": weight,
                "stock": stock,
                "template": template,
            },
        )


_BRAND_D_SLUGS_SQL = "('brand_d_wheel', 'brand_d_polaroid', 'brand_d_confirmation')"


def downgrade() -> None:
    # Откат снимает прохождения Бренд D вместе с их наградами и спинами (FK RESTRICT),
    # затем призы колеса и сами активности. Только dev и тесты: на проде не откатываем.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM rewards WHERE attempt_id IN ("
            "  SELECT t.id FROM attempts t JOIN activities a ON a.id = t.activity_id"
            f"  WHERE a.slug IN {_BRAND_D_SLUGS_SQL})"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM attempts WHERE activity_id IN ("
            f"  SELECT id FROM activities WHERE slug IN {_BRAND_D_SLUGS_SQL})"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM wheel_prizes WHERE activity_id IN "
            "(SELECT id FROM activities WHERE slug = 'brand_d_wheel')"
        )
    )
    for slug, _display_name, _kind, _limits in _ACTIVITIES:
        bind.execute(sa.text("DELETE FROM activities WHERE slug = :slug"), {"slug": slug})
