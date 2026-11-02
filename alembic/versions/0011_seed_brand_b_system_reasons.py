"""seed brand_b system reject reasons (ТОП-1 ревью фазы 3c)

Revision ID: 0011_seed_brand_b_system_reasons
Revises: 0010_seed_brand_b_reject_reasons
Create Date: 2026-10-18

Системные причины сбоя приёмки UGC для Бренд B (правило 5: подписи живут в БД, в коде их нет).
Ключ `activities.limits.system_reject_reasons`: слаг -> подпись. Слаги пишет воркер приёмки
(mime_mismatch, file_not_found, file_too_large, ingest_dead, consent_revoked); подпись попадает в
текст уведомления ingest_failed. Это отдельный словарь от `reject_reasons` модератора (0010).

Миграция только дописывает ключ в limits, схема не меняется. Повтор безопасен: `||` перезаписывает
ключ тем же значением. `downgrade` убирает ключ.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0011_seed_brand_b_system_reasons"
down_revision: str | None = "0010_seed_brand_b_reject_reasons"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_ACTIVITY_SLUG = "brand_b_photo_subscription"

_SYSTEM_REJECT_REASONS: dict[str, Any] = {
    "mime_mismatch": "Формат файла не поддерживается",
    "file_not_found": "Файл не удалось получить от Telegram, пришлите ещё раз",
    "file_too_large": "Файл слишком большой",
    "ingest_dead": "Техническая ошибка при приёмке файла",
    "consent_revoked": "Согласие отозвано",
}

_PATCH_LIMITS = sa.text(
    "UPDATE activities SET limits = limits || CAST(:patch AS jsonb), updated_at = now() "
    "WHERE slug = :slug"
)

_DROP_SYSTEM_REJECT_REASONS = sa.text(
    "UPDATE activities SET limits = limits - 'system_reject_reasons', updated_at = now() "
    "WHERE slug = :slug"
)


def upgrade() -> None:
    op.get_bind().execute(
        _PATCH_LIMITS,
        {
            "patch": json.dumps(
                {"system_reject_reasons": _SYSTEM_REJECT_REASONS}, ensure_ascii=False
            ),
            "slug": _ACTIVITY_SLUG,
        },
    )


def downgrade() -> None:
    op.get_bind().execute(_DROP_SYSTEM_REJECT_REASONS, {"slug": _ACTIVITY_SLUG})
