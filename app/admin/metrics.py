"""Экран /admin/metrics (фаза 7c): регистрации, завершённые прохождения и выдачи по дням.

Тонкий слой (правило 3): разбор query, вызов core.metrics, рендер. Только агрегаты, ПДн нет.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import Response

from app.admin.templating import render_page
from app.api.deps import current_admin, get_clock, get_uow
from app.api.middleware.csrf import attach_csrf_cookie, current_csrf_value
from app.config import Settings, get_settings
from app.core.metrics import MAX_DAYS, MIN_DAYS, daily_metrics
from app.core.ports.clock import ClockPort
from app.core.types.dto import AdminRef
from app.db.unit_of_work import UnitOfWork

router = APIRouter()


@router.get("/metrics")
async def metrics_page(
    request: Request,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    days: Annotated[int, Query(ge=MIN_DAYS, le=MAX_DAYS)] = 7,
    brand_slug: Annotated[str | None, Query(max_length=100)] = None,
) -> Response:
    slug = brand_slug or None
    rows = await daily_metrics(uow.repositories.metrics, days, slug, clock.now())
    csrf_value, is_new = current_csrf_value(request)
    response = render_page(
        "metrics_list.html",
        title="Метрики",
        admin=admin,
        csrf=csrf_value,
        rows=rows,
        days=days,
        brand_slug=slug or "",
        brand_filtered=slug is not None,
    )
    if is_new:
        attach_csrf_cookie(response, csrf_value, secure=settings.cookie_secure)
    return response
