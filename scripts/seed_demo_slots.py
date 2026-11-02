"""Dev-скрипт: демо-слоты записи на сегодня (решение координатора, п.6). Не часть миграций.

Миграция 0012 сидит только активности; слоты в prod заводит администратор через /admin/schedule.
Запуск из корня проекта (только dev/тестовая БД): APP_ENV=dev py -3 -m scripts.seed_demo_slots
Идемпотентно: для активности, где на сегодня уже есть слоты, ничего не добавляется.

Бренд A: 10 слотов по 15 минут с 12:00 МСК. Бренд C: 8 слотов по 15 минут с 13:00 МСК.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.common.time_utils import EVENT_TIMEZONE

# (slug активности из сида 0012, час начала, число слотов), шаг 15 минут.
PLAN: tuple[tuple[str, int, int], ...] = (
    ("brand_a_diagnostics_booking", 12, 10),
    ("brand_c_stylist_booking", 13, 8),
)
SLOT_MINUTES = 15
DEV_ENVIRONMENTS: Final = frozenset({"dev", "test"})


def ensure_dev_environment(app_env: str) -> None:
    """Ревью фазы 4 (M2), fail-closed: демо-слоты пишем только при явном APP_ENV=dev или test.

    Пустое или любое иное значение (в том числе prod, где переменную забыли задать) - отказ.
    """
    if app_env.strip().lower() not in DEV_ENVIRONMENTS:
        raise RuntimeError("seed_demo_slots is dev-only: set APP_ENV=dev (or test)")


def seed(engine: Engine, day: date) -> dict[str, int]:
    """Добавляет слоты дня `day` (по Москве). Возвращает число созданных слотов по slug."""
    zone = ZoneInfo(EVENT_TIMEZONE)
    created: dict[str, int] = {}
    with engine.begin() as conn:
        for slug, hour, count in PLAN:
            activity_id = conn.execute(
                text("SELECT id FROM activities WHERE slug = :s AND kind = 'booking'"),
                {"s": slug},
            ).scalar_one_or_none()
            if activity_id is None:
                raise RuntimeError(f"нет активности {slug}: примените миграции до head")
            day_start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
            day_end = day_start + timedelta(days=1)
            existing = conn.execute(
                text(
                    "SELECT count(*) FROM slots WHERE activity_id = :a "
                    "AND start_at >= :ds AND start_at < :de"
                ),
                {"a": activity_id, "ds": day_start, "de": day_end},
            ).scalar_one()
            if existing:
                created[slug] = 0
                continue
            first = datetime.combine(day, datetime.min.time(), tzinfo=zone).replace(hour=hour)
            for index in range(count):
                conn.execute(
                    text(
                        "INSERT INTO slots (activity_id, start_at, duration_minutes, kind, "
                        "created_at, updated_at) VALUES (:a, :ts, :d, 'bookable', now(), now())"
                    ),
                    {
                        "a": activity_id,
                        "ts": first + timedelta(minutes=SLOT_MINUTES * index),
                        "d": SLOT_MINUTES,
                    },
                )
            created[slug] = count
    return created


def main() -> None:
    from app.config import get_settings

    settings = get_settings()
    ensure_dev_environment(settings.app_env)
    engine = create_engine(settings.database_url, future=True)
    try:
        today = datetime.now(ZoneInfo(EVENT_TIMEZONE)).date()
        result = seed(engine, today)
    finally:
        engine.dispose()
    for slug, count in result.items():
        print(f"{slug}: создано слотов {count}")


if __name__ == "__main__":
    main()
