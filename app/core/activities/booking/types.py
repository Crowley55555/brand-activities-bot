"""Типы booking (ADR-33): результаты use-case и строки чтения записей и расписания.

ПДн здесь нет: только id, статусы, время и настройки активности (правило 9).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from app.core.types.ids import (
    ActivityId,
    AppointmentId,
    AttemptId,
    BrandId,
    ChannelAccountId,
    SlotId,
    UserId,
)

AppointmentStatus = Literal[
    "booked",
    "reminded",
    "arrived",
    "late",
    "cancelled_by_user",
    "cancelled_auto",
]
ReminderOutcome = Literal[
    "reminded",
    "not_booked",
    "too_late",
    "reminders_disabled",
    "no_telegram_channel",
]
AutoCancelOutcome = Literal["cancelled_auto", "already_resolved"]


@dataclass(frozen=True)
class BookResult:
    appointment_id: AppointmentId
    slot_start: datetime
    status: AppointmentStatus
    remind_before_minutes: int
    has_reminders: bool


@dataclass(frozen=True)
class CancelResult:
    status: AppointmentStatus


@dataclass(frozen=True)
class AppointmentDetail:
    """Запись вместе с прохождением, слотом и настройками активности (одним запросом)."""

    appointment_id: AppointmentId
    attempt_id: AttemptId
    slot_id: SlotId
    status: AppointmentStatus
    remind_at: datetime
    auto_cancel_at: datetime
    reminded_at: datetime | None
    arrived_at: datetime | None
    user_id: UserId
    channel_account_id: ChannelAccountId
    activity_id: ActivityId
    brand_id: BrandId
    activity_name: str
    limits: Mapping[str, Any]
    slot_start: datetime


@dataclass(frozen=True)
class BookingActivity:
    """Активность kind='booking' для выпадающего списка администратора."""

    activity_id: ActivityId
    brand_id: BrandId
    display_name: str


@dataclass(frozen=True)
class ScheduleRow:
    """Строка экрана /admin/schedule: слот и живая запись на него (если есть)."""

    slot_id: SlotId
    start_at: datetime
    duration_minutes: int
    activity_id: ActivityId
    activity_name: str
    brand_id: BrandId
    appointment_id: AppointmentId | None
    appointment_status: AppointmentStatus | None
    arrived_at: datetime | None
