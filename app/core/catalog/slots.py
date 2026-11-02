"""Слоты записи: чтение свободных на сегодня (канон 03 Q6, 08 §3 - владелец core.catalog).

«Сегодня» - календарный день по Москве (EVENT_TIMEZONE, как attempt_date). Слот свободен, если
он bookable, не удалён, ещё не начался (start_at > now) и на него нет активной записи
(booked/reminded/arrived). Занятость считается запросом; поля «занят» в slots нет (03 §1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from app.common.time_utils import EVENT_TIMEZONE, attempt_date_for
from app.core.types.ids import ActivityId, SlotId

SlotKind = Literal["bookable", "break"]


@dataclass(frozen=True)
class SlotRef:
    """Слот расписания. Удалённые и занятые сюда не попадают (фильтр в запросе)."""

    id: SlotId
    activity_id: ActivityId
    start_at: datetime
    duration_minutes: int
    kind: SlotKind


class SlotReadRepo(Protocol):
    async def list_free_bookable(
        self,
        activity_id: ActivityId,
        day_start: datetime,
        day_end: datetime,
        now: datetime,
    ) -> list[SlotRef]:
        """Свободные bookable-слоты активности в [day_start, day_end) с start_at > now."""
        ...


def event_zone() -> ZoneInfo:
    return ZoneInfo(EVENT_TIMEZONE)


def today_window(now: datetime) -> tuple[datetime, datetime]:
    """Границы календарного дня по Москве, в котором находится `now`: [начало, конец)."""
    zone = event_zone()
    local_day = now.astimezone(zone).date()
    day_start = datetime.combine(local_day, time(0), tzinfo=zone)
    return day_start, day_start + timedelta(days=1)


def is_same_event_day(moment: datetime, now: datetime) -> bool:
    """Совпадает ли московская дата момента `moment` с датой `now`."""
    return attempt_date_for(moment) == attempt_date_for(now)


async def list_free_slots_today(
    repo: SlotReadRepo, activity_id: ActivityId, now: datetime
) -> list[SlotRef]:
    """Свободные на сегодня слоты активности (T11): прошедшие, занятые и завтрашние не входят."""
    day_start, day_end = today_window(now)
    return await repo.list_free_bookable(activity_id, day_start, day_end, now)
