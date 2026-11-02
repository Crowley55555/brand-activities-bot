"""Экран расписания /admin/schedule (ADR-38, фаза 4): слоты сегодня, «пришёл», «не пришёл», слот.

Тонкий слой (правило 3): читает параметры и форму, вызывает core.activities.booking, отвечает
HTML или 302 на расписание. Права на бренды проверяет ядро (Forbidden brand_access_denied), здесь
только список брендов администратора. CSRF - app-level csrf_guard (ADR-22); на странице - токен.
Время в форме - местное московское («2026-10-12T15:00»), в ядро уходит aware-datetime.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Final
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from app.admin.templating import render_page
from app.api.deps import current_admin, get_clock, get_uow
from app.api.middleware.csrf import attach_csrf_cookie, current_csrf_value
from app.common.errors import NotFound, ValidationError
from app.common.time_utils import EVENT_TIMEZONE
from app.config import get_settings
from app.core.activities.booking import (
    ScheduleRow,
    booking_activities_for,
    bulk_create_slots,
    create_slot,
    list_schedule,
    live_appointment_for_slot,
    mark_arrived,
    mark_no_show,
)
from app.core.admin_access import list_authorized_brands
from app.core.ports.clock import ClockPort
from app.core.types.dto import AdminRef
from app.core.types.ids import ActivityId, AdminId, BrandId, SlotId
from app.db.unit_of_work import Repositories, UnitOfWork

router = APIRouter()

SCHEDULE_PATH: Final[str] = "/admin/schedule"
_STATUS_LABELS: Final[dict[str, str]] = {
    "booked": "записан",
    "reminded": "напомнили",
    "arrived": "пришёл",
}
_MARKABLE: Final[frozenset[str]] = frozenset({"booked", "reminded"})


async def _admin_brands(repos: Repositories, admin: AdminRef) -> frozenset[BrandId]:
    brands = await list_authorized_brands(repos.admins, AdminId(admin.id))
    return frozenset(BrandId(int(brand.id)) for brand in brands)


def _parse_local_start(raw: str) -> datetime:
    """Поле «Начало» - местное время Москвы без смещения; смещение добавляем здесь."""
    try:
        naive = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValidationError("bad_request") from exc
    if naive.tzinfo is not None:
        raise ValidationError("bad_request")
    return naive.replace(tzinfo=ZoneInfo(EVENT_TIMEZONE))


_ERROR_LABELS: Final[dict[str, str]] = {
    "bad_request": "неверные параметры формы",
    "slot_in_past": "начало слота в прошлом (укажите время позже текущего)",
    "slot_not_today": "начало и конец должны быть в пределах сегодняшнего дня (МСК)",
    "step_out_of_range": "шаг должен быть от 5 до 120 минут",
    "end_before_start": "конец раньше или равен началу",
    "range_too_large": "диапазон больше 24 часов",
    "too_many_slots": "запрошено больше 100 слотов за раз",
    "activity_not_booking": "выбранная активность не поддерживает слоты",
    "activity_inactive": "активность неактивна",
    "activity_not_found": "активность не найдена",
    "brand_access_denied": "нет прав на этот бренд",
    "duration_invalid": "длительность слота должна быть 1..240 минут",
    "slot_duplicate": "слот с таким началом уже существует",
}


def _flash(request: Request) -> dict[str, str] | None:
    """Итог bulk из query (?created=N&conflicts=M | ?error=код); значения экранирует Jinja."""
    params = request.query_params
    if "error" in params:
        code = params["error"][:40]
        label = _ERROR_LABELS.get(code, code)
        return {"kind": "error", "text": f"Слоты не созданы: {label}"}
    created, conflicts = params.get("created"), params.get("conflicts", "0")
    if created is not None and created.isdigit() and conflicts.isdigit():
        return {"kind": "ok", "text": f"Создано {created} слотов, пропущено дублей: {conflicts}"}
    return None


def _row_view(row: ScheduleRow) -> dict[str, Any]:
    local = row.start_at.astimezone(ZoneInfo(EVENT_TIMEZONE))
    status = row.appointment_status
    return {
        "slot_id": int(row.slot_id),
        "time": local.strftime("%H:%M"),
        "activity": row.activity_name,
        "status": _STATUS_LABELS.get(status or "", "свободен"),
        "can_mark": status in _MARKABLE,
    }


@router.get("/schedule")
async def schedule_page(
    request: Request,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> Response:
    settings = get_settings()
    repos = uow.repositories
    brand_ids: Sequence[BrandId] = sorted(await _admin_brands(repos, admin))
    now = clock.now()
    rows = await list_schedule(repos, brand_ids, now)
    activities = await booking_activities_for(repos, brand_ids)
    csrf_value, is_new = current_csrf_value(request)
    response = render_page(
        "schedule.html",
        title="Расписание",
        admin=admin,
        csrf=csrf_value,
        today=now.astimezone(ZoneInfo(EVENT_TIMEZONE)).strftime("%d.%m.%Y"),
        flash=_flash(request),
        rows=[_row_view(row) for row in rows],
        activities=[
            {"id": int(item.activity_id), "name": item.display_name} for item in activities
        ],
    )
    if is_new:
        attach_csrf_cookie(response, csrf_value, secure=settings.cookie_secure)
    return response


@router.post("/schedule/{slot_id}/mark_arrived")
async def mark_arrived_route(
    slot_id: int,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[Any, Depends(get_clock)],
) -> Response:
    repos = uow.repositories
    brand_ids = await _admin_brands(repos, admin)
    live = await live_appointment_for_slot(repos, SlotId(slot_id))
    if live is None:
        raise NotFound("appointment_not_found")
    await mark_arrived(repos, live.appointment_id, AdminId(admin.id), brand_ids, clock.now())
    await uow.commit()  # F2: 302 - только после фиксации прихода и листовки
    return RedirectResponse(SCHEDULE_PATH, status_code=302)


@router.post("/schedule/{slot_id}/mark_no_show")
async def mark_no_show_route(
    slot_id: int,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[Any, Depends(get_clock)],
) -> Response:
    repos = uow.repositories
    brand_ids = await _admin_brands(repos, admin)
    live = await live_appointment_for_slot(repos, SlotId(slot_id))
    if live is None:
        raise NotFound("appointment_not_found")
    await mark_no_show(repos, live.appointment_id, AdminId(admin.id), brand_ids, clock.now())
    await uow.commit()
    return RedirectResponse(SCHEDULE_PATH, status_code=302)


@router.post("/schedule/create_slot")
async def create_slot_route(
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[Any, Depends(get_clock)],
    activity_id: Annotated[str, Form()] = "",
    start_at: Annotated[str, Form()] = "",
    duration_minutes: Annotated[str, Form()] = "15",
) -> Response:
    try:
        activity = ActivityId(int(activity_id))
        duration = int(duration_minutes)
    except ValueError as exc:
        raise ValidationError("bad_request") from exc
    local_start = _parse_local_start(start_at)
    repos = uow.repositories
    brand_ids = await _admin_brands(repos, admin)
    await create_slot(repos, activity, local_start, duration, brand_ids, clock.now())
    await uow.commit()
    return RedirectResponse(SCHEDULE_PATH, status_code=302)


@router.post("/schedule/bulk_create_slots")
async def bulk_create_slots_route(
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[Any, Depends(get_clock)],
    activity_id: Annotated[str, Form()] = "",
    day_start_local: Annotated[str, Form()] = "",
    day_end_local: Annotated[str, Form()] = "",
    step_minutes: Annotated[str, Form()] = "",
) -> Response:
    """Только парсинг формы и редирект; лимиты и права - в core.bulk_create_slots."""
    try:
        activity = ActivityId(int(activity_id))
        step = int(step_minutes)
        day_start = _parse_local_start(day_start_local)
        day_end = _parse_local_start(day_end_local)
        repos = uow.repositories
        brand_ids = await _admin_brands(repos, admin)
        result = await bulk_create_slots(
            repos, activity, day_start, day_end, step, brand_ids, clock.now()
        )
    except ValueError:
        return RedirectResponse(f"{SCHEDULE_PATH}?error=bad_request", status_code=302)
    except ValidationError as exc:
        # Ничего не вставлено (проверки до вставок), commit не нужен.
        return RedirectResponse(f"{SCHEDULE_PATH}?error={exc.code}", status_code=302)
    await uow.commit()
    return RedirectResponse(
        f"{SCHEDULE_PATH}?created={result.created}&conflicts={result.conflicts}", status_code=302
    )
