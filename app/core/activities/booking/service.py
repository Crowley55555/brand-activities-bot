"""Use-case'ы booking (ADR-33..36, решения координатора по фазе 4).

Порядок book_slot: согласие (правило 1, первой строкой) -> проверка «один раз в день» по любой
строке прохождения (решение п.3 C) -> слот (есть, своя активность, bookable, ещё не начался,
сегодня по Москве) -> start_attempt с override_expires_at = слот + 10 минут (п.4) -> INSERT
appointment (partial unique - защита гонки, T3) -> enqueue джобов: напоминание только при
has_reminders (п.2), авто-отмена всегда.

Переходы appointments - атомарные UPDATE с предикатом по статусу: повтор или гонка джоба и
сканера даёт «ничего не изменилось», а не дубль (п.1 C). Сообщения гостю ставятся в той же
транзакции, что и переход (outbox-паттерн, 04 §5.1).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from app.common.errors import ConflictError, Forbidden, NotFound, ValidationError
from app.common.time_utils import attempt_date_for
from app.core.activities.booking.repository import BookingRepos
from app.core.activities.booking.types import (
    AppointmentDetail,
    AutoCancelOutcome,
    BookingActivity,
    BookResult,
    CancelResult,
    ReminderOutcome,
    ScheduleRow,
)
from app.core.attempts import close_attempt, start_attempt
from app.core.attempts.repository import AttemptRepo
from app.core.audit import log_admin_action
from app.core.catalog import get_activity_by_slug
from app.core.catalog.slots import is_same_event_day, today_window
from app.core.consent import require_consent
from app.core.jobs import enqueue
from app.core.rewards import grant_on_completion
from app.core.types.dto import GrantSpec
from app.core.types.enums import AttemptStatus
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

# Окно напоминания и авто-отмены по канону 02 §12: за 5 минут до слота и через 5 минут после.
# Значения читаются из activities.limits (правило 5); это - запасное значение, если ключа нет.
DEFAULT_REMIND_BEFORE_MINUTES = 5
DEFAULT_AUTO_CANCEL_AFTER_MINUTES = 5
# Срок прохождения booking: слот + 10 минут (решение п.4), а не stale_ttl_minutes.
ATTEMPT_EXPIRY_AFTER_SLOT = timedelta(minutes=10)
TELEGRAM: Final = "telegram"
LIVE_STATUSES = ("booked", "reminded", "arrived")


def _int_limit(limits: Mapping[str, Any], key: str, default: int) -> int:
    value = limits.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError("booking_limit_invalid")
    return value


def _has_reminders(limits: Mapping[str, Any]) -> bool:
    return bool(limits.get("has_reminders", True))


def _chat_id(external_id: str) -> int:
    try:
        return int(external_id)
    except ValueError as exc:
        raise ValidationError("bad_external_id") from exc


def _require_brand(brand_id: BrandId, admin_brand_ids: frozenset[BrandId]) -> None:
    if brand_id not in admin_brand_ids:
        raise Forbidden("brand_access_denied")


async def _close_attempt_quietly(
    attempts: AttemptRepo,
    attempt_id: AttemptId,
    status: AttemptStatus,
    summary: str,
    now: datetime,
) -> None:
    """Закрыть прохождение; уже закрытое (гонка с другим путём) - не ошибка."""
    try:
        await close_attempt(attempts, attempt_id, status, summary, now)
    except ConflictError as exc:
        if exc.code != "already_closed":
            raise


async def _enqueue_guest_message(
    repos: BookingRepos,
    external_id: str,
    template: str,
    appointment_id: AppointmentId,
) -> None:
    """telegram_send с ids в context (3c-конвенция): текст собирает claim_outbox из БД."""
    await enqueue(
        repos.jobs,
        "telegram_send",
        {
            "chat_id": _chat_id(external_id),
            "template": template,
            "context": {"appointment_id": int(appointment_id)},
        },
        idempotency_key=f"{template}:{int(appointment_id)}",
    )


async def _notify_auto_cancelled(
    repos: BookingRepos, detail: AppointmentDetail, appointment_id: AppointmentId
) -> None:
    channel = await repos.channels.find_active_by_user(detail.user_id, TELEGRAM)
    if channel is None:
        return
    await _enqueue_guest_message(
        repos, channel.external_id, "appointment_auto_cancelled", appointment_id
    )


def _grants_on_arrival(limits: Mapping[str, Any]) -> list[GrantSpec]:
    """Листовка при приходе (решение п.11): награда leaflet, заголовок из limits."""
    if not limits.get("has_leaflet"):
        return []
    title = str(limits.get("leaflet_title", "")).strip()
    if not title:
        raise ValidationError("leaflet_title_missing")
    return [GrantSpec(reward_type="leaflet", title=title)]


async def book_slot(
    repos: BookingRepos,
    user_id: UserId,
    activity_slug: str,
    channel_account_id: ChannelAccountId,
    slot_id: SlotId,
    now: datetime,
) -> BookResult:
    """Бронирование слота гостем (T1-T3, T6, T6b, решения п.1-4, п.8)."""
    await require_consent(repos.consents, user_id)  # правило 1: первой строкой
    activity = await get_activity_by_slug(repos.catalog, activity_slug)
    if activity.kind != "booking":
        raise ValidationError("activity_kind_mismatch")

    # Один раз в день - ДО start_attempt: иначе проверка увидит собственную строку (п.3 C).
    existing = await repos.attempts.find_today_by_user_activity(
        user_id, activity.id, attempt_date_for(now)
    )
    if existing is not None:
        raise ConflictError("already_passed_today")

    slot = await repos.slots.get(slot_id)
    if slot is None or slot.activity_id != activity.id or slot.kind != "bookable":
        raise ConflictError("slot_taken")
    if slot.start_at <= now or not is_same_event_day(slot.start_at, now):
        raise ConflictError("slot_taken")

    started = await start_attempt(
        repos.consents,
        repos.catalog,
        repos.attempts,
        user_id,
        activity_slug,
        channel_account_id,
        now,
        expected_kind="booking",
        override_expires_at=slot.start_at + ATTEMPT_EXPIRY_AFTER_SLOT,
    )

    remind_before = _int_limit(
        activity.limits, "remind_before_minutes", DEFAULT_REMIND_BEFORE_MINUTES
    )
    auto_after = _int_limit(
        activity.limits, "auto_cancel_after_minutes", DEFAULT_AUTO_CANCEL_AFTER_MINUTES
    )
    remind_at = slot.start_at - timedelta(minutes=remind_before)
    auto_cancel_at = slot.start_at + timedelta(minutes=auto_after)

    appointment_id = await repos.appointments.insert_booked(
        started.attempt_id, slot.id, remind_at, auto_cancel_at
    )
    if appointment_id is None:
        raise ConflictError("slot_taken")

    key = int(appointment_id)
    has_reminders = _has_reminders(activity.limits)
    if has_reminders:
        await enqueue(
            repos.jobs,
            "notify_appointment_reminder",
            {"appointment_id": key},
            run_after=remind_at,
            idempotency_key=f"reminder:{key}",
        )
    await enqueue(
        repos.jobs,
        "auto_cancel_appointment",
        {"appointment_id": key},
        run_after=auto_cancel_at,
        idempotency_key=f"autocancel:{key}",
    )
    return BookResult(
        appointment_id=appointment_id,
        slot_start=slot.start_at,
        status="booked",
        remind_before_minutes=remind_before,
        has_reminders=has_reminders,
    )


async def cancel_by_user(
    repos: BookingRepos,
    appointment_id: AppointmentId,
    user_id: UserId,
    now: datetime,
) -> CancelResult:
    """Отмена гостем (T4): атомарно; джобы остаются, обработчики проверят статус."""
    detail = await repos.appointments.get_detail(appointment_id)
    if detail is None:
        raise NotFound("appointment_not_found")
    if detail.user_id != user_id:
        raise Forbidden("attempt_not_owned")
    if not await repos.appointments.mark_cancelled_by_user(appointment_id, now):
        raise ConflictError("not_cancellable")
    await _close_attempt_quietly(
        repos.attempts, detail.attempt_id, "cancelled", "booking:cancelled_by_user", now
    )
    return CancelResult(status="cancelled_by_user")


async def mark_reminded(
    repos: BookingRepos, appointment_id: AppointmentId, now: datetime
) -> ReminderOutcome:
    """Напоминание за 5 минут (T7): booked -> reminded и сообщение, или терминальный исход.

    Используют и джоб notify_appointment_reminder, и сканер scheduler_tick (п.1 C).
    """
    detail = await repos.appointments.get_detail(appointment_id)
    if detail is None or detail.status != "booked":
        return "not_booked"
    if not _has_reminders(detail.limits):
        return "reminders_disabled"
    if now > detail.slot_start:  # п.9: опоздали - не напоминаем
        return "too_late"
    channel = await repos.channels.find_active_by_user(detail.user_id, TELEGRAM)
    if channel is None:
        return "no_telegram_channel"
    if not await repos.appointments.mark_reminded(appointment_id, now):
        return "not_booked"
    await _enqueue_guest_message(repos, channel.external_id, "appointment_reminder", appointment_id)
    return "reminded"


async def mark_auto_cancelled(
    repos: BookingRepos, appointment_id: AppointmentId, now: datetime
) -> AutoCancelOutcome:
    """Авто-отмена (T9, T10): booked|reminded без прихода -> cancelled_auto, попытка cancelled."""
    detail = await repos.appointments.get_detail(appointment_id)
    if detail is None:
        return "already_resolved"
    if not await repos.appointments.mark_auto_cancelled(appointment_id, now):
        return "already_resolved"
    await _close_attempt_quietly(
        repos.attempts, detail.attempt_id, "cancelled", "booking:cancelled_auto", now
    )
    await _notify_auto_cancelled(repos, detail, appointment_id)
    return "cancelled_auto"


async def mark_arrived(
    repos: BookingRepos,
    appointment_id: AppointmentId,
    admin_id: AdminId,
    admin_brand_ids: frozenset[BrandId],
    now: datetime,
) -> None:
    """«Пришёл» у стойки (T5, T14): arrived, попытка completed, листовка в той же транзакции."""
    detail = await repos.appointments.get_detail(appointment_id)
    if detail is None:
        raise NotFound("appointment_not_found")
    _require_brand(detail.brand_id, admin_brand_ids)
    if not await repos.appointments.mark_arrived(appointment_id, admin_id, now):
        raise ConflictError("not_arrivable")
    await _close_attempt_quietly(
        repos.attempts, detail.attempt_id, "completed", "booking:arrived", now
    )
    grants = _grants_on_arrival(detail.limits)
    if grants:
        await grant_on_completion(repos.rewards, detail.attempt_id, grants, now)
        # Листовка уходит отдельным шаблоном с id записи: рендер reward_notice знает только
        # промокод и ваучер, для награды leaflet он дал бы reward_missing.
        channel = await repos.channels.find_active_by_user(detail.user_id, TELEGRAM)
        if channel is not None:
            await _enqueue_guest_message(
                repos, channel.external_id, "appointment_leaflet", appointment_id
            )
    await log_admin_action(
        repos.audit, admin_id, "mark_arrived", "appointments", int(appointment_id), now
    )


async def mark_no_show(
    repos: BookingRepos,
    appointment_id: AppointmentId,
    admin_id: AdminId,
    admin_brand_ids: frozenset[BrandId],
    now: datetime,
) -> None:
    """Не пришёл (ADR-38, п.10): cancelled_auto и то же сообщение, что у авто-отмены (п.4)."""
    detail = await repos.appointments.get_detail(appointment_id)
    if detail is None:
        raise NotFound("appointment_not_found")
    _require_brand(detail.brand_id, admin_brand_ids)
    if not await repos.appointments.mark_auto_cancelled(appointment_id, now):
        raise ConflictError("not_markable")
    await _close_attempt_quietly(
        repos.attempts, detail.attempt_id, "cancelled", "booking:no_show", now
    )
    await _notify_auto_cancelled(repos, detail, appointment_id)
    await log_admin_action(
        repos.audit, admin_id, "mark_no_show", "appointments", int(appointment_id), now
    )


async def live_appointment_for_slot(
    repos: BookingRepos, slot_id: SlotId
) -> AppointmentDetail | None:
    return await repos.appointments.find_live_for_slot(slot_id)


async def booking_activities_for(
    repos: BookingRepos, admin_brand_ids: Sequence[BrandId]
) -> list[BookingActivity]:
    if not admin_brand_ids:
        return []
    return await repos.slots.list_booking_activities(admin_brand_ids)


async def list_schedule(
    repos: BookingRepos, admin_brand_ids: Sequence[BrandId], now: datetime
) -> list[ScheduleRow]:
    """Экран /admin/schedule: слоты сегодня (Москва) только по брендам администратора."""
    if not admin_brand_ids:
        return []
    day_start, day_end = today_window(now)
    return await repos.slots.list_schedule(admin_brand_ids, day_start, day_end)


async def create_slot(
    repos: BookingRepos,
    activity_id: ActivityId,
    start_at: datetime,
    duration_minutes: int,
    admin_brand_ids: frozenset[BrandId],
    now: datetime,
) -> SlotId:
    """Одиночный слот на сегодня (решение п.7): kind='bookable', без массовой генерации."""
    activity = await repos.slots.get_booking_activity(activity_id)
    if activity is None:
        raise NotFound("activity_not_found")
    _require_brand(activity.brand_id, admin_brand_ids)
    if not 1 <= duration_minutes <= 240:
        raise ValidationError("duration_invalid")
    if start_at <= now:
        raise ValidationError("slot_in_past")
    if not is_same_event_day(start_at, now):
        raise ValidationError("slot_not_today")
    return await repos.slots.insert(activity_id, start_at, duration_minutes)
