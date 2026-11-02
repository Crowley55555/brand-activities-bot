"""Сканеры booking (решение координатора, п.1 C и п.2): страховка к джобам.

scan_reminders_due / scan_auto_cancels_due выбирают записи, у которых наступил срок, и проводят
их теми же use-case'ами, что и джобы (mark_reminded / mark_auto_cancelled). Джоб, который
уже сработал или пропущен, даёт «ничего не изменилось», а не дубль сообщения.

Напоминания сканер берёт только у активностей с has_reminders (у выключенных - нет, п.2) и только
если слот ещё не начался (иначе напоминание бессмысленно, п.9).
Вызывается из app/workers/scheduler_tick.py раз в минуту (08 §2, канон 04 §1.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.activities.booking.repository import BookingRepos
from app.core.activities.booking.service import mark_auto_cancelled, mark_reminded
from app.core.types.ids import AppointmentId


@dataclass(frozen=True)
class DueScanReport:
    reminded: int
    auto_cancelled: int


async def scan_reminders_due(repos: BookingRepos, now: datetime, limit: int) -> list[int]:
    ids = await repos.appointments.scan_reminders_due(now, limit)
    return [int(i) for i in ids]


async def scan_auto_cancels_due(repos: BookingRepos, now: datetime, limit: int) -> list[int]:
    ids = await repos.appointments.scan_auto_cancels_due(now, limit)
    return [int(i) for i in ids]


async def run_due_scans(repos: BookingRepos, now: datetime, limit: int = 100) -> DueScanReport:
    """Один проход сканеров внутри транзакции вызывающего (scheduler_tick)."""
    reminded = 0
    for appointment_id in await scan_reminders_due(repos, now, limit):
        if await mark_reminded(repos, AppointmentId(appointment_id), now) == "reminded":
            reminded += 1
    auto_cancelled = 0
    for appointment_id in await scan_auto_cancels_due(repos, now, limit):
        if await mark_auto_cancelled(repos, AppointmentId(appointment_id), now) == "cancelled_auto":
            auto_cancelled += 1
    return DueScanReport(reminded=reminded, auto_cancelled=auto_cancelled)
