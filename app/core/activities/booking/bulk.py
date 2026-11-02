"""Массовое создание слотов шаблоном «на сегодня» (фаза 7d): от начала до конца дня с шагом.

Длительность слота = шаг. Лимиты - бизнес-факты, живут здесь (правило 5). Все проверки диапазона
идут ДО первой вставки, поэтому при ValidationError в БД ничего не остаётся. Повторный bulk
не плодит дубли: слот с той же парой (активность, начало) считается конфликтом и пропускается.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from app.common.errors import ValidationError
from app.core.activities.booking.repository import BookingRepos
from app.core.activities.booking.service import create_slot
from app.core.catalog.slots import is_same_event_day
from app.core.types.ids import ActivityId, BrandId, SlotId

MIN_STEP_MINUTES: Final[int] = 5
MAX_STEP_MINUTES: Final[int] = 120
MAX_RANGE: Final[timedelta] = timedelta(hours=24)
MAX_SLOTS_PER_BATCH: Final[int] = 100


@dataclass(frozen=True)
class BulkCreateResult:
    created: int
    conflicts: int
    created_slot_ids: list[SlotId]


async def bulk_create_slots(
    repos: BookingRepos,
    activity_id: ActivityId,
    day_start: datetime,
    day_end: datetime,
    step_minutes: int,
    admin_brand_ids: frozenset[BrandId],
    now: datetime,
) -> BulkCreateResult:
    """Слоты с start = day_start + k*step < day_end. Права и «сегодня» - как у create_slot."""
    if not MIN_STEP_MINUTES <= step_minutes <= MAX_STEP_MINUTES:
        raise ValidationError("step_out_of_range")
    if day_end <= day_start:
        raise ValidationError("end_before_start")
    if day_end - day_start > MAX_RANGE:
        raise ValidationError("range_too_large")
    step = timedelta(minutes=step_minutes)
    starts: list[datetime] = []
    cursor = day_start
    while cursor < day_end:
        starts.append(cursor)
        if len(starts) > MAX_SLOTS_PER_BATCH:
            raise ValidationError("too_many_slots")
        cursor += step
    # Предпроверка границ, чтобы падение не случилось посреди пачки (create_slot проверит ещё раз).
    if starts[0] <= now:
        raise ValidationError("slot_in_past")
    if not is_same_event_day(starts[0], now) or not is_same_event_day(starts[-1], now):
        raise ValidationError("slot_not_today")

    created: list[SlotId] = []
    conflicts = 0
    for start_at in starts:
        if await repos.slots.slot_exists(activity_id, start_at):
            conflicts += 1
            continue
        created.append(
            await create_slot(repos, activity_id, start_at, step_minutes, admin_brand_ids, now)
        )
    return BulkCreateResult(created=len(created), conflicts=conflicts, created_slot_ids=created)
