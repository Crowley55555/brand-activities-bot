from __future__ import annotations

from datetime import date, datetime
from typing import Final
from zoneinfo import ZoneInfo

# Часовой пояс мероприятия. Это не настройка: он зашит в CHECK-ограничение БД
# `attempts_date_matches_started` (alembic/versions/0001_initial.py:
# attempt_date = (started_at AT TIME ZONE 'Europe/Moscow')::date), а «один раз в
# день» (partial unique attempts_once_per_day) считается по этой же дате. Смена
# пояса = новая миграция схемы, поэтому константа живёт рядом с БД-инвариантом,
# а не в app/config.py.
EVENT_TIMEZONE: Final[str] = "Europe/Moscow"


def attempt_date_for(moment: datetime) -> date:
    """Локальная дата мероприятия для момента `moment` (aware datetime, обычно UTC).

    Значение обязано совпадать с тем, что посчитает CHECK в БД, иначе INSERT в
    attempts будет отклонён.
    """
    if moment.tzinfo is None:
        raise ValueError("attempt_date_for requires a timezone-aware datetime")
    return moment.astimezone(ZoneInfo(EVENT_TIMEZONE)).date()
