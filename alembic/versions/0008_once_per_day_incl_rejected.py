"""attempts_once_per_day включает rejected (ADR-19, фаза 3a)

Revision ID: 0008_once_per_day_incl_rejected
Revises: 0007_seed_brand_b_activity
Create Date: 2026-10-16

Решение оркестратора (N5, ADR-19): после финального отказа (лимит повторов исчерпан,
прохождение rejected) гость не стартует новое прохождение той же активности в тот же день,
иначе лимит повторов обходится. Поэтому `rejected` входит в партиальный unique-индекс
`attempts_once_per_day`. `cancelled` (технический сбой ingest, N3) и `expired` остаются вне
индекса: начать заново можно.

Имя ревизии короче 32 символов: alembic хранит её в alembic_version.version_num (VARCHAR(32)).
Имя из промта (0008_attempts_once_per_day_include_rejected, 43 символа) упало бы при записи.

Индекс пересоздаётся без CONCURRENTLY: прода ещё нет. На проде такую ревизию выкатывают
отдельно, без транзакции (03 §5.6). Если в базе есть пара (user, activity, date), где одно
прохождение rejected, а другое в старом списке статусов, upgrade упадёт на уникальности;
такие данные чистят до миграции (в dev-базах их нет).

downgrade возвращает прежний предикат. Безопасен: старый индекс - подмножество нового.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_once_per_day_incl_rejected"
down_revision: str | None = "0007_seed_brand_b_activity"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_INDEX_COLUMNS: Sequence[str] = ("user_id", "activity_id", "attempt_date")
_OLD_PREDICATE = "status IN ('in_progress','pending_moderation','approved','completed')"
_NEW_PREDICATE = "status IN ('in_progress','pending_moderation','approved','completed','rejected')"


def _recreate_once_per_day(predicate: str) -> None:
    op.drop_index("attempts_once_per_day", table_name="attempts")
    op.create_index(
        "attempts_once_per_day",
        "attempts",
        list(_INDEX_COLUMNS),
        unique=True,
        postgresql_where=sa.text(predicate),
    )


def upgrade() -> None:
    _recreate_once_per_day(_NEW_PREDICATE)


def downgrade() -> None:
    _recreate_once_per_day(_OLD_PREDICATE)
