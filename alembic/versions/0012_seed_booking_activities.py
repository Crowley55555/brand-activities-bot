"""seed booking activities: Бренд A-диагностика и Бренд C-стилист (ADR-35, решение п.6)

Revision ID: 0012_seed_booking_activities
Revises: 0011_seed_brand_b_system_reasons
Create Date: 2026-10-19

Две активности kind='booking' (правило 5: slug, названия и настройки живут только здесь).
Имя ревизии короче 32 символов: alembic хранит его в alembic_version.version_num VARCHAR(32)
(см. docstring 0008); имя из ADR-35 (34 символа) не записалось бы.

Слоты сюда не входят (решение координатора, п.6): демо-слоты создаёт dev-скрипт
scripts/seed_demo_slots.py, в prod слоты заводит администратор через /admin/schedule.

Ключи activities.limits, которые читает ядро core.activities.booking:
* has_reminders - напоминание за remind_before_minutes до слота (Бренд A: нет, стенд целый день);
* remind_before_minutes / auto_cancel_after_minutes - сдвиги от slot.start_at (по 5 минут);
* stale_ttl_minutes - TTL прохождения без слота (для booking не используется, см. start_attempt);
* has_leaflet / leaflet_url / leaflet_title - листовка Бренд C: награда типа leaflet при приходе
  (URL отдаёт StaticFiles приложения; заголовок в коде не живёт, правило 5).

Идемпотентно: ON CONFLICT (slug) DO NOTHING. downgrade удаляет строки; при прохождениях
FK RESTRICT остановит откат (на проде downgrade не запускаем).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0012_seed_booking_activities"
down_revision: str | None = "0011_seed_brand_b_system_reasons"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_BOOKINGS: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    (
        "brand_a",
        "brand_a_diagnostics_booking",
        "Диагностика кожи",
        {
            "max_per_day": 1,
            "requires_moderation": False,
            "stale_ttl_minutes": 180,
            "has_reminders": False,
        },
    ),
    (
        "brand_c",
        "brand_c_stylist_booking",
        "Запись к стилисту",
        {
            "max_per_day": 1,
            "requires_moderation": False,
            "stale_ttl_minutes": 120,
            "has_reminders": True,
            "remind_before_minutes": 5,
            "auto_cancel_after_minutes": 5,
            "has_leaflet": True,
            "leaflet_url": "/static/leaflets/brand_c.pdf",
            "leaflet_title": "Листовка Бренд C",
        },
    ),
)

_INSERT_ACTIVITY = sa.text(
    """
    INSERT INTO activities (brand_id, slug, display_name, kind, is_active, limits,
                            created_at, updated_at)
    SELECT b.id, :slug, :display_name, 'booking', TRUE, CAST(:limits AS jsonb),
           now(), now()
    FROM brands b
    WHERE b.slug = :brand_slug
    ON CONFLICT (slug) DO NOTHING
    """
)


def upgrade() -> None:
    bind = op.get_bind()
    for brand_slug, slug, display_name, limits in _BOOKINGS:
        bind.execute(
            _INSERT_ACTIVITY,
            {
                "slug": slug,
                "display_name": display_name,
                "limits": json.dumps(limits, ensure_ascii=False),
                "brand_slug": brand_slug,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    for _brand_slug, slug, _display_name, _limits in _BOOKINGS:
        bind.execute(sa.text("DELETE FROM activities WHERE slug = :slug"), {"slug": slug})
