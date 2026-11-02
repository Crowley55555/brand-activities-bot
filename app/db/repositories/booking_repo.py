"""SQL-реализация портов booking: слоты (SlotRepo) и записи (AppointmentRepo).

Переходы статусов - одиночные UPDATE с предикатом по текущему статусу и RETURNING: повтор или
гонка даёт пустой результат, а не дубль (04 §5.1). Занятость слота - запросом (03 Q6), поля нет.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.activities.booking.types import (
    AppointmentDetail,
    AppointmentStatus,
    BookingActivity,
    ScheduleRow,
)
from app.core.catalog.slots import SlotKind, SlotRef
from app.core.jobs.outbox import AppointmentText
from app.core.types.ids import (
    ActivityId,
    AdminId,
    AppointmentId,
    AttemptId,
    BrandId,
    ChannelAccountId,
    SlotId,
    UserId,
)

_LIVE = "('booked','reminded','arrived')"


def _slot_ref(row: Any) -> SlotRef:
    return SlotRef(
        id=SlotId(int(row.id)),
        activity_id=ActivityId(int(row.activity_id)),
        start_at=row.start_at,
        duration_minutes=int(row.duration_minutes),
        kind=cast(SlotKind, row.kind),
    )


def _detail(row: Any) -> AppointmentDetail:
    return AppointmentDetail(
        appointment_id=AppointmentId(int(row.appointment_id)),
        attempt_id=AttemptId(int(row.attempt_id)),
        slot_id=SlotId(int(row.slot_id)),
        status=cast(AppointmentStatus, row.status),
        remind_at=row.remind_at,
        auto_cancel_at=row.auto_cancel_at,
        reminded_at=row.reminded_at,
        arrived_at=row.arrived_at,
        user_id=UserId(int(row.user_id)),
        channel_account_id=ChannelAccountId(int(row.channel_account_id)),
        activity_id=ActivityId(int(row.activity_id)),
        brand_id=BrandId(int(row.brand_id)),
        activity_name=str(row.activity_name),
        limits=dict(row.limits or {}),
        slot_start=row.slot_start,
    )


class SqlSlotRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_free_bookable(
        self,
        activity_id: ActivityId,
        day_start: datetime,
        day_end: datetime,
        now: datetime,
    ) -> list[SlotRef]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT s.id, s.activity_id, s.start_at, s.duration_minutes, s.kind "
                    "FROM slots s "
                    "WHERE s.activity_id = :a AND s.kind = 'bookable' AND s.deleted_at IS NULL "
                    "AND s.start_at > :now AND s.start_at >= :ds AND s.start_at < :de "
                    "AND NOT EXISTS (SELECT 1 FROM appointments ap "
                    f"  WHERE ap.slot_id = s.id AND ap.status IN {_LIVE}) "
                    "ORDER BY s.start_at, s.id"
                ),
                {"a": int(activity_id), "now": now, "ds": day_start, "de": day_end},
            )
        ).all()
        return [_slot_ref(r) for r in rows]

    async def get(self, slot_id: SlotId) -> SlotRef | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, activity_id, start_at, duration_minutes, kind FROM slots "
                    "WHERE id = :id AND deleted_at IS NULL"
                ),
                {"id": int(slot_id)},
            )
        ).one_or_none()
        return _slot_ref(row) if row is not None else None

    async def insert(
        self,
        activity_id: ActivityId,
        start_at: datetime,
        duration_minutes: int,
    ) -> SlotId:
        new_id = (
            await self._session.execute(
                text(
                    "INSERT INTO slots (activity_id, start_at, duration_minutes, kind, "
                    "created_at, updated_at) "
                    "VALUES (:a, :ts, :d, 'bookable', now(), now()) RETURNING id"
                ),
                {"a": int(activity_id), "ts": start_at, "d": duration_minutes},
            )
        ).scalar_one()
        return SlotId(int(new_id))

    async def slot_exists(self, activity_id: ActivityId, start_at: datetime) -> bool:
        found = (
            await self._session.execute(
                text(
                    "SELECT 1 FROM slots WHERE activity_id = :a AND start_at = :ts "
                    "AND deleted_at IS NULL LIMIT 1"
                ),
                {"a": int(activity_id), "ts": start_at},
            )
        ).first()
        return found is not None

    async def get_booking_activity(self, activity_id: ActivityId) -> BookingActivity | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, brand_id, display_name FROM activities "
                    "WHERE id = :id AND kind = 'booking' AND is_active AND deleted_at IS NULL"
                ),
                {"id": int(activity_id)},
            )
        ).one_or_none()
        if row is None:
            return None
        return BookingActivity(
            activity_id=ActivityId(int(row.id)),
            brand_id=BrandId(int(row.brand_id)),
            display_name=str(row.display_name),
        )

    async def list_booking_activities(self, brand_ids: Sequence[BrandId]) -> list[BookingActivity]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT id, brand_id, display_name FROM activities "
                    "WHERE kind = 'booking' AND is_active AND deleted_at IS NULL "
                    "AND brand_id = ANY(:brands) ORDER BY display_name, id"
                ),
                {"brands": [int(b) for b in brand_ids]},
            )
        ).all()
        return [
            BookingActivity(
                activity_id=ActivityId(int(r.id)),
                brand_id=BrandId(int(r.brand_id)),
                display_name=str(r.display_name),
            )
            for r in rows
        ]

    async def list_schedule(
        self,
        brand_ids: Sequence[BrandId],
        day_start: datetime,
        day_end: datetime,
    ) -> list[ScheduleRow]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT s.id AS slot_id, s.start_at, s.duration_minutes, "
                    "act.id AS activity_id, act.display_name AS activity_name, "
                    "act.brand_id, ap.id AS appointment_id, ap.status AS appointment_status, "
                    "ap.arrived_at "
                    "FROM slots s "
                    "JOIN activities act ON act.id = s.activity_id "
                    f"LEFT JOIN appointments ap ON ap.slot_id = s.id AND ap.status IN {_LIVE} "
                    "WHERE s.deleted_at IS NULL AND s.kind = 'bookable' "
                    "AND s.start_at >= :ds AND s.start_at < :de "
                    "AND act.kind = 'booking' AND act.brand_id = ANY(:brands) "
                    "ORDER BY s.start_at, s.id"
                ),
                {"ds": day_start, "de": day_end, "brands": [int(b) for b in brand_ids]},
            )
        ).all()
        return [
            ScheduleRow(
                slot_id=SlotId(int(r.slot_id)),
                start_at=r.start_at,
                duration_minutes=int(r.duration_minutes),
                activity_id=ActivityId(int(r.activity_id)),
                activity_name=str(r.activity_name),
                brand_id=BrandId(int(r.brand_id)),
                appointment_id=(
                    AppointmentId(int(r.appointment_id)) if r.appointment_id is not None else None
                ),
                appointment_status=(
                    cast(AppointmentStatus, r.appointment_status)
                    if r.appointment_status is not None
                    else None
                ),
                arrived_at=r.arrived_at,
            )
            for r in rows
        ]


class SqlAppointmentRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_booked(
        self,
        attempt_id: AttemptId,
        slot_id: SlotId,
        remind_at: datetime,
        auto_cancel_at: datetime,
    ) -> AppointmentId | None:
        # Явная цель конфликта (ревью L1): частичный unique-индекс appointments_slot_active.
        new_id = (
            await self._session.execute(
                text(
                    "INSERT INTO appointments (attempt_id, slot_id, status, remind_at, "
                    "auto_cancel_at, created_at, updated_at) "
                    "VALUES (:att, :slot, 'booked', :remind, :cancel, now(), now()) "
                    "ON CONFLICT (slot_id) "
                    "WHERE status IN ('booked', 'reminded', 'arrived') "
                    "DO NOTHING RETURNING id"
                ),
                {
                    "att": int(attempt_id),
                    "slot": int(slot_id),
                    "remind": remind_at,
                    "cancel": auto_cancel_at,
                },
            )
        ).scalar_one_or_none()
        return AppointmentId(int(new_id)) if new_id is not None else None

    async def get_detail(self, appointment_id: AppointmentId) -> AppointmentDetail | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT ap.id AS appointment_id, ap.attempt_id, ap.slot_id, ap.status, "
                    "ap.remind_at, ap.auto_cancel_at, ap.reminded_at, ap.arrived_at, "
                    "t.user_id, t.channel_account_id, t.activity_id, "
                    "act.brand_id, act.display_name AS activity_name, act.limits, "
                    "s.start_at AS slot_start "
                    "FROM appointments ap "
                    "JOIN attempts t ON t.id = ap.attempt_id "
                    "JOIN slots s ON s.id = ap.slot_id "
                    "JOIN activities act ON act.id = t.activity_id "
                    "WHERE ap.id = :id"
                ),
                {"id": int(appointment_id)},
            )
        ).one_or_none()
        return _detail(row) if row is not None else None

    async def find_live_for_slot(self, slot_id: SlotId) -> AppointmentDetail | None:
        appointment_id = (
            await self._session.execute(
                text(
                    "SELECT id FROM appointments WHERE slot_id = :s "
                    f"AND status IN {_LIVE} ORDER BY id LIMIT 1"
                ),
                {"s": int(slot_id)},
            )
        ).scalar_one_or_none()
        if appointment_id is None:
            return None
        return await self.get_detail(AppointmentId(int(appointment_id)))

    async def describe(self, appointment_id: int) -> AppointmentText | None:
        """Текст уведомления: название активности, слот и limits (порт OutboxAppointments)."""
        row = (
            await self._session.execute(
                text(
                    "SELECT act.display_name AS activity_name, act.limits, "
                    "s.start_at AS slot_start "
                    "FROM appointments ap "
                    "JOIN slots s ON s.id = ap.slot_id "
                    "JOIN attempts t ON t.id = ap.attempt_id "
                    "JOIN activities act ON act.id = t.activity_id "
                    "WHERE ap.id = :id"
                ),
                {"id": appointment_id},
            )
        ).one_or_none()
        if row is None:
            return None
        return AppointmentText(
            activity_name=str(row.activity_name),
            slot_start=row.slot_start,
            limits=dict(row.limits or {}),
        )

    async def _transition(self, sql: str, params: dict[str, Any]) -> bool:
        row = (await self._session.execute(text(sql), params)).scalar_one_or_none()
        return row is not None

    async def mark_reminded(self, appointment_id: AppointmentId, now: datetime) -> bool:
        return await self._transition(
            "UPDATE appointments SET status = 'reminded', reminded_at = :now, updated_at = now() "
            "WHERE id = :id AND status = 'booked' AND reminded_at IS NULL RETURNING id",
            {"id": int(appointment_id), "now": now},
        )

    async def mark_cancelled_by_user(self, appointment_id: AppointmentId, now: datetime) -> bool:
        return await self._transition(
            "UPDATE appointments SET status = 'cancelled_by_user', cancelled_at = :now, "
            "updated_at = now() WHERE id = :id AND status IN ('booked','reminded') RETURNING id",
            {"id": int(appointment_id), "now": now},
        )

    async def mark_auto_cancelled(self, appointment_id: AppointmentId, now: datetime) -> bool:
        return await self._transition(
            "UPDATE appointments SET status = 'cancelled_auto', cancelled_at = :now, "
            "updated_at = now() WHERE id = :id AND status IN ('booked','reminded') "
            "AND arrived_at IS NULL RETURNING id",
            {"id": int(appointment_id), "now": now},
        )

    async def mark_arrived(
        self,
        appointment_id: AppointmentId,
        admin_id: AdminId,
        now: datetime,
    ) -> bool:
        return await self._transition(
            "UPDATE appointments SET status = 'arrived', arrived_at = :now, "
            "arrived_marked_by_admin_id = :admin, updated_at = now() "
            "WHERE id = :id AND status IN ('booked','reminded') RETURNING id",
            {"id": int(appointment_id), "now": now, "admin": int(admin_id)},
        )

    async def scan_reminders_due(self, now: datetime, limit: int) -> list[AppointmentId]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT ap.id FROM appointments ap "
                    "JOIN slots s ON s.id = ap.slot_id "
                    "JOIN attempts t ON t.id = ap.attempt_id "
                    "JOIN activities act ON act.id = t.activity_id "
                    "WHERE ap.status = 'booked' AND ap.reminded_at IS NULL "
                    "AND ap.remind_at <= :now AND s.start_at >= :now "
                    "AND COALESCE(act.limits ->> 'has_reminders', 'true') = 'true' "
                    "ORDER BY ap.remind_at, ap.id LIMIT :limit"
                ),
                {"now": now, "limit": limit},
            )
        ).all()
        return [AppointmentId(int(r.id)) for r in rows]

    async def scan_auto_cancels_due(self, now: datetime, limit: int) -> list[AppointmentId]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT id FROM appointments WHERE status IN ('booked','reminded') "
                    "AND auto_cancel_at <= :now AND arrived_at IS NULL "
                    "ORDER BY auto_cancel_at, id LIMIT :limit"
                ),
                {"now": now, "limit": limit},
            )
        ).all()
        return [AppointmentId(int(r.id)) for r in rows]
