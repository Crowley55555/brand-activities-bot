"""seed brands

Revision ID: 0002_seed_brands
Revises: 0001_initial
Create Date: 2026-10-13

Пять участвующих брендов (slug + display_name + tagline). Правило 5:
значения живут только здесь, не в коде. Миграция идемпотентная —
`ON CONFLICT (slug) DO NOTHING`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_seed_brands"
down_revision: str | None = "0001_initial"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_BRANDS: list[tuple[str, str, str]] = [
    ("brand_a", "Бренд A", "Код кожи"),
    ("brand_b", "Бренд B", "Код beauty-ритма"),
    ("brand_c", "Бренд C", "Запись к стилисту"),
    ("brand_d", "Бренд D", "Колесо момента"),
    ("brand_e", "Бренд E", "Код настроения"),
]


_INSERT_SQL = sa.text(
    """
    INSERT INTO brands (slug, display_name, tagline, is_active,
                        created_at, updated_at)
    VALUES (:slug, :display_name, :tagline, TRUE, now(), now())
    ON CONFLICT (slug) DO NOTHING
    """
)

_DELETE_SQL = sa.text("DELETE FROM brands WHERE slug = :slug")


def upgrade() -> None:
    bind = op.get_bind()
    for slug, display_name, tagline in _BRANDS:
        bind.execute(
            _INSERT_SQL,
            {"slug": slug, "display_name": display_name, "tagline": tagline},
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug, _display_name, _tagline in _BRANDS:
        bind.execute(_DELETE_SQL, {"slug": slug})
