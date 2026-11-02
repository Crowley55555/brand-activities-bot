"""attempts.expires_at и частичный индекс attempts_expire_due

Revision ID: 0005_attempts_expires_at
Revises: 0004_seed_brand_a_products
Create Date: 2026-10-15

Канон 03 §1 (attempts) описывает `expires_at` = `started_at + stale_ttl_minutes` для
прохождения `in_progress` и индекс `attempts_expire_due`. В 0001 колонки не было, поэтому
добавляем её этой миграцией. По ней джоб `expire_stale_attempts` (04 §1.3, п. 12a) переводит
брошенные прохождения в `expired` и освобождает «один раз в день».

Колонка nullable: прохождения, созданные до миграции, остаются с NULL, и джоб их не снимает.
`downgrade` удаляет индекс и колонку.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_attempts_expires_at"
down_revision: str | None = "0004_seed_brand_a_products"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "attempts",
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "attempts_expire_due",
        "attempts",
        ["expires_at"],
        postgresql_where=sa.text("status = 'in_progress'"),
    )


def downgrade() -> None:
    op.drop_index("attempts_expire_due", table_name="attempts")
    op.drop_column("attempts", "expires_at")
