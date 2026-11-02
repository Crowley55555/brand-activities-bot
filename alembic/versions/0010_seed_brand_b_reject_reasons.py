"""seed brand_b reject reasons (Q4, фаза 3b)

Revision ID: 0010_seed_brand_b_reject_reasons
Revises: 0009_ugc_active_unique
Create Date: 2026-10-17

Причины отказа модерации UGC для Бренд B (правило 5: список живёт в БД, в коде его нет).
Ключ `activities.limits.reject_reasons`: слаг -> подпись. Подпись видна модератору в форме,
слаг уходит в задачу notify_ugc_rejected и проверяется ядром (reason_slug_unknown).

Миграция только дописывает ключ в limits; сид 0007 не меняется. Повтор безопасен: `||`
перезаписывает ключ тем же значением. `downgrade` убирает ключ.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0010_seed_brand_b_reject_reasons"
down_revision: str | None = "0009_ugc_active_unique"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_ACTIVITY_SLUG = "brand_b_photo_subscription"

_REJECT_REASONS: dict[str, Any] = {
    "low_quality": "Низкое качество фото",
    "no_post_link": "Нет ссылки на пост",
    "no_subscription": "Нет подписки",
    "off_topic": "Фото не по теме",
    "other": "Другое",
}

_PATCH_LIMITS = sa.text(
    "UPDATE activities SET limits = limits || CAST(:patch AS jsonb), updated_at = now() "
    "WHERE slug = :slug"
)

_DROP_REJECT_REASONS = sa.text(
    "UPDATE activities SET limits = limits - 'reject_reasons', updated_at = now() "
    "WHERE slug = :slug"
)


def upgrade() -> None:
    op.get_bind().execute(
        _PATCH_LIMITS,
        {
            "patch": json.dumps({"reject_reasons": _REJECT_REASONS}, ensure_ascii=False),
            "slug": _ACTIVITY_SLUG,
        },
    )


def downgrade() -> None:
    op.get_bind().execute(_DROP_REJECT_REASONS, {"slug": _ACTIVITY_SLUG})
