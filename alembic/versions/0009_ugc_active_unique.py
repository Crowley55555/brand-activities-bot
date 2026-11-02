"""Одна активная подача UGC на прохождение: частичный unique-индекс (F1, ревью фазы 3a)

Revision ID: 0009_ugc_active_unique
Revises: 0008_once_per_day_incl_rejected
Create Date: 2026-10-16

Ревью фазы 3a, блокер F1: два параллельных resubmit одного прохождения могли создать две
активные строки (receiving/pending/approved). Ядро берёт блокировку строки прохождения
(`attempts.lock_for_update`), а индекс - страховка на случай, если блокировку забыли.

Имя ревизии короче 32 символов: alembic хранит её в alembic_version.version_num (VARCHAR(32)).
Имя из задания (0009_ugc_submission_active_unique, 33 символа) не подходит.

Индекс без CONCURRENTLY: прода ещё нет. На проде - отдельной ревизией без транзакции (03 §5.6).
downgrade удаляет индекс: прежнее поведение без защиты от гонки.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_ugc_active_unique"
down_revision: str | None = "0008_once_per_day_incl_rejected"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ugc_submission_active",
        "ugc_submissions",
        ["attempt_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('receiving','pending','approved')"),
    )


def downgrade() -> None:
    op.drop_index("ugc_submission_active", table_name="ugc_submissions")
