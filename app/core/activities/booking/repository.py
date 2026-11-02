"""Порты booking: слоты, записи и набор репозиториев, нужный use-case'ам.

BookingRepos - структурный протокол: контейнер UnitOfWork его удовлетворяет, core не знает
про SQLAlchemy (правило 3). Свойства только для чтения - контейнер отдаёт конкретные классы.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.core.activities.booking.types import (
    AppointmentDetail,
    BookingActivity,
    ScheduleRow,
)
from app.core.attempts.repository import AttemptRepo
from app.core.audit.repository import AuditRepo
from app.core.catalog.repository import CatalogRepo
from app.core.catalog.slots import SlotReadRepo, SlotRef
from app.core.channels.repository import ChannelAccountRepo
from app.core.consent.repository import ConsentRepo
from app.core.jobs.repository import JobRepo
from app.core.rewards.repository import RewardRepo
from app.core.types.ids import ActivityId, AdminId, AppointmentId, AttemptId, BrandId, SlotId


class SlotRepo(SlotReadRepo, Protocol):
    async def get(self, slot_id: SlotId) -> SlotRef | None: ...

    async def insert(
        self,
        activity_id: ActivityId,
        start_at: datetime,
        duration_minutes: int,
    ) -> SlotId:
        """Новый слот kind='bookable'. CHECK длительности 1..240 - в БД."""
        ...

    async def slot_exists(self, activity_id: ActivityId, start_at: datetime) -> bool:
        """Есть ли неудалённый слот активности с этим началом (unique-индекса в БД нет)."""
        ...

    async def get_booking_activity(self, activity_id: ActivityId) -> BookingActivity | None:
        """Активность только если она kind='booking' и не удалена."""
        ...

    async def list_booking_activities(
        self, brand_ids: Sequence[BrandId]
    ) -> list[BookingActivity]: ...

    async def list_schedule(
        self,
        brand_ids: Sequence[BrandId],
        day_start: datetime,
        day_end: datetime,
    ) -> list[ScheduleRow]:
        """Слоты дня по брендам с живой записью (booked/reminded/arrived), по времени старта."""
        ...


class AppointmentRepo(Protocol):
    async def insert_booked(
        self,
        attempt_id: AttemptId,
        slot_id: SlotId,
        remind_at: datetime,
        auto_cancel_at: datetime,
    ) -> AppointmentId | None:
        """Вставка booked. None - слот уже занят активной записью (partial unique в БД)."""
        ...

    async def get_detail(self, appointment_id: AppointmentId) -> AppointmentDetail | None: ...

    async def find_live_for_slot(self, slot_id: SlotId) -> AppointmentDetail | None:
        """Запись на слот в статусе booked/reminded/arrived (для кнопок администратора)."""
        ...

    async def mark_reminded(self, appointment_id: AppointmentId, now: datetime) -> bool:
        """booked -> reminded, атомарно: только если reminded_at IS NULL."""
        ...

    async def mark_cancelled_by_user(
        self, appointment_id: AppointmentId, now: datetime
    ) -> bool: ...

    async def mark_auto_cancelled(self, appointment_id: AppointmentId, now: datetime) -> bool:
        """booked|reminded -> cancelled_auto, только если гость не отмечен как пришедший."""
        ...

    async def mark_arrived(
        self,
        appointment_id: AppointmentId,
        admin_id: AdminId,
        now: datetime,
    ) -> bool:
        """booked|reminded -> arrived с фиксацией администратора."""
        ...

    async def scan_reminders_due(self, now: datetime, limit: int) -> list[AppointmentId]: ...

    async def scan_auto_cancels_due(self, now: datetime, limit: int) -> list[AppointmentId]: ...


class BookingRepos(Protocol):
    """Контейнер репозиториев для use-case'ов booking (структурно совместим с UoW)."""

    @property
    def consents(self) -> ConsentRepo: ...

    @property
    def catalog(self) -> CatalogRepo: ...

    @property
    def attempts(self) -> AttemptRepo: ...

    @property
    def slots(self) -> SlotRepo: ...

    @property
    def appointments(self) -> AppointmentRepo: ...

    @property
    def jobs(self) -> JobRepo: ...

    @property
    def channels(self) -> ChannelAccountRepo: ...

    @property
    def rewards(self) -> RewardRepo: ...

    @property
    def audit(self) -> AuditRepo: ...
