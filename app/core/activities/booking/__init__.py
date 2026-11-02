"""Публичная дверь booking (08 §3): снаружи ходят только через этот пакет."""

from __future__ import annotations

from app.core.activities.booking.bulk import BulkCreateResult, bulk_create_slots
from app.core.activities.booking.repository import (
    AppointmentRepo,
    BookingRepos,
    SlotRepo,
)
from app.core.activities.booking.scanner import DueScanReport, run_due_scans
from app.core.activities.booking.service import (
    book_slot,
    booking_activities_for,
    cancel_by_user,
    create_slot,
    list_schedule,
    live_appointment_for_slot,
    mark_arrived,
    mark_auto_cancelled,
    mark_no_show,
    mark_reminded,
)
from app.core.activities.booking.types import (
    AppointmentDetail,
    AppointmentStatus,
    AutoCancelOutcome,
    BookingActivity,
    BookResult,
    CancelResult,
    ReminderOutcome,
    ScheduleRow,
)
from app.core.catalog.slots import SlotRef, list_free_slots_today

__all__ = [
    "AppointmentDetail",
    "AppointmentRepo",
    "AppointmentStatus",
    "AutoCancelOutcome",
    "BookResult",
    "BookingActivity",
    "BookingRepos",
    "BulkCreateResult",
    "CancelResult",
    "DueScanReport",
    "ReminderOutcome",
    "ScheduleRow",
    "SlotRef",
    "SlotRepo",
    "book_slot",
    "booking_activities_for",
    "bulk_create_slots",
    "cancel_by_user",
    "create_slot",
    "list_free_slots_today",
    "list_schedule",
    "live_appointment_for_slot",
    "mark_arrived",
    "mark_auto_cancelled",
    "mark_no_show",
    "mark_reminded",
    "run_due_scans",
]
