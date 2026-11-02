"""seed brand_b activity (ADR-17)

Revision ID: 0007_seed_brand_b_activity
Revises: 0006_ugc_jobs_schema
Create Date: 2026-10-16

Активность Бренд B «Фото и подписка» (kind='ugc'). Правило 5: slug, название, лимиты,
шаблоны кодов и название награды живут только здесь, в коде их нет.

Ключи `activities.limits`, которые читает ядро `core.activities.ugc`:
* `max_per_day`, `requires_moderation`, `max_file_bytes`, `allowed_mimes`,
  `max_resubmits` - ограничения (валидация файла - фаза 3b/3c);
* `voucher_title` - название ваучера «Напиток дня»;
* `voucher_code_template` - код ваучера `VOUCHER-{random8}` (ваучер тоже имеет код,
  CHECK rewards_code_matches_type);
* `promocode_template` - промокод `BRAND_B-{random8}` (как BRAND_A в 0003, ADR-13).

Идемпотентно: `ON CONFLICT (slug) DO NOTHING`. `downgrade` удаляет строку; если по
активности уже есть прохождения, FK RESTRICT остановит откат (на проде downgrade не
запускаем).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0007_seed_brand_b_activity"
down_revision: str | None = "0006_ugc_jobs_schema"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_BRAND_SLUG = "brand_b"
_ACTIVITY_SLUG = "brand_b_photo_subscription"
_ACTIVITY_NAME = "Фото и подписка"

_LIMITS: dict[str, Any] = {
    "max_per_day": 1,
    "requires_moderation": True,
    "max_file_bytes": 5242880,
    "allowed_mimes": ["image/jpeg", "image/png", "image/webp"],
    "max_resubmits": 1,
    "voucher_title": "Напиток дня",
    "promocode_template": "BRAND_B-{random8}",
    "voucher_code_template": "VOUCHER-{random8}",
}

_INSERT_ACTIVITY = sa.text(
    """
    INSERT INTO activities (brand_id, slug, display_name, kind, is_active, limits,
                            created_at, updated_at)
    SELECT b.id, :slug, :display_name, 'ugc', TRUE, CAST(:limits AS jsonb),
           now(), now()
    FROM brands b
    WHERE b.slug = :brand_slug
    ON CONFLICT (slug) DO NOTHING
    """
)


def upgrade() -> None:
    op.get_bind().execute(
        _INSERT_ACTIVITY,
        {
            "slug": _ACTIVITY_SLUG,
            "display_name": _ACTIVITY_NAME,
            "limits": json.dumps(_LIMITS, ensure_ascii=False),
            "brand_slug": _BRAND_SLUG,
        },
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM activities WHERE slug = :slug"),
        {"slug": _ACTIVITY_SLUG},
    )
