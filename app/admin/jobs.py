"""DLQ-UI /admin/jobs/dead (фаза 10b-2, I-1): список `jobs.status='dead'` с
кнопками «повторить» и «похоронить». Тонкий слой (правило 3): разбор query,
вызов `core.jobs.dlq`, рендер. Правило 10: cookie-сессия + CSRF double-submit
(app-level `csrf_guard`, ADR-22). Правило 2: сырой payload задачи наружу не
выводится — у `telegram_send` в payload `chat_id`, у ingest — `telegram_file_id`.
Для DLQ достаточно `kind` + `last_error`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from starlette.responses import RedirectResponse, Response

from app.admin.templating import render_page
from app.api.deps import current_admin, get_clock, get_uow
from app.api.middleware.csrf import attach_csrf_cookie, current_csrf_value
from app.common.errors import ConflictError, ValidationError
from app.config import Settings, get_settings
from app.core.jobs.dlq import (
    BURY_REASON_SLUGS,
    bury_dead,
    list_dead,
    revive_dead,
)
from app.core.jobs.types import JOB_KINDS
from app.core.ports.clock import ClockPort
from app.core.types.dto import AdminRef
from app.core.types.ids import AdminId, JobId
from app.db.unit_of_work import UnitOfWork

router = APIRouter()
logger = logging.getLogger("app.admin.jobs")

DEAD_PATH: Final[str] = "/admin/jobs/dead"
PAGE_SIZE: Final[int] = 50
_TIME_FORMAT: Final[str] = "%d.%m.%Y %H:%M"
_ERROR_PREVIEW: Final[int] = 300  # last_error режем, чтобы страница была читаемая
_KINDS_SORTED: Final[tuple[str, ...]] = tuple(sorted(JOB_KINDS))
_REASONS_SORTED: Final[tuple[str, ...]] = tuple(sorted(BURY_REASON_SLUGS))


@dataclass(frozen=True)
class DeadItem:
    """Строка таблицы DLQ без payload (правило 2)."""

    id: int
    kind: str
    attempts: int
    max_attempts: int
    last_error: str
    updated_label: str
    updated_iso: str
    idempotency_key: str


def _summary(last_error: str | None) -> str:
    if last_error is None:
        return ""
    text = last_error.strip()
    if len(text) <= _ERROR_PREVIEW:
        return text
    return text[:_ERROR_PREVIEW] + "…"


def _item(row: object) -> DeadItem:
    # row — DeadJobRow (dataclass из core/jobs/repository.py).
    return DeadItem(
        id=int(row.id),  # type: ignore[attr-defined]
        kind=str(row.kind),  # type: ignore[attr-defined]
        attempts=int(row.attempts),  # type: ignore[attr-defined]
        max_attempts=int(row.max_attempts),  # type: ignore[attr-defined]
        last_error=_summary(row.last_error),  # type: ignore[attr-defined]
        updated_label=row.updated_at.strftime(_TIME_FORMAT),  # type: ignore[attr-defined]
        updated_iso=row.updated_at.isoformat(),  # type: ignore[attr-defined]
        idempotency_key=row.idempotency_key or "",  # type: ignore[attr-defined]
    )


def _redirect_back(kind: str, page: int, *, flash: dict[str, str] | None = None) -> Response:
    params: dict[str, str] = {}
    if kind:
        params["kind"] = kind
    if page > 1:
        params["page"] = str(page)
    if flash:
        params.update(flash)
    qs = urlencode(params)
    url = DEAD_PATH if not qs else f"{DEAD_PATH}?{qs}"
    return RedirectResponse(url, status_code=302)


def _flash(request: Request) -> dict[str, str] | None:
    """Короткое сообщение после revive/bury: ?ok=revive | ?ok=bury | ?error=<slug>."""
    params = request.query_params
    ok = params.get("ok")
    err = params.get("error")
    if ok in {"revive", "bury"}:
        return {"level": "ok", "kind": ok}
    if err:
        return {"level": "error", "kind": err[:64]}
    return None


@router.get("/jobs/dead")
async def dead_page(
    request: Request,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    settings: Annotated[Settings, Depends(get_settings)],
    kind: Annotated[str, Query(max_length=64)] = "",
    page: Annotated[int, Query(ge=1, le=100_000)] = 1,
) -> Response:
    """Страница dead-задач: фильтр по kind, пагинация, формы revive/bury."""
    kind_filter: str | None = kind if kind in JOB_KINDS else None
    offset = (page - 1) * PAGE_SIZE
    rows, total = await list_dead(
        uow.repositories.jobs, kind_filter, PAGE_SIZE, offset
    )
    csrf_value, is_new = current_csrf_value(request)
    params: dict[str, str] = {}
    if kind_filter:
        params["kind"] = kind_filter
    response = render_page(
        "jobs_dead.html",
        title="DLQ · очередь",
        admin=admin,
        csrf=csrf_value,
        items=[_item(row) for row in rows],
        total=total,
        page=page,
        has_prev=page > 1,
        has_next=page * PAGE_SIZE < total,
        kind=kind_filter or "",
        kinds=_KINDS_SORTED,
        reasons=_REASONS_SORTED,
        base_query=urlencode(params),
        flash=_flash(request),
    )
    if is_new:
        attach_csrf_cookie(response, csrf_value, secure=settings.cookie_secure)
    return response


@router.post("/jobs/dead/{job_id}/revive")
async def revive_action(
    job_id: int,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
    kind: Annotated[str, Form()] = "",
    page: Annotated[int, Form(ge=1, le=100_000)] = 1,
) -> Response:
    """dead → queued + audit. На конфликте 'not_dead' — редирект с flash."""
    try:
        await revive_dead(
            uow.repositories.jobs,
            uow.repositories.audit,
            AdminId(admin.id),
            JobId(job_id),
            clock.now(),
        )
    except ConflictError as exc:
        logger.info(
            "jobs_dead_revive_rejected",
            extra={
                "event": "jobs_dead_revive_rejected",
                "admin_id": admin.id,
                "job_id": job_id,
                "reason": exc.code,
            },
        )
        return _redirect_back(kind, page, flash={"error": exc.code})
    await uow.commit()  # F2: 302 уходит после фиксации revive + audit.
    logger.info(
        "jobs_dead_revive_ok",
        extra={
            "event": "jobs_dead_revive_ok",
            "admin_id": admin.id,
            "job_id": job_id,
        },
    )
    return _redirect_back(kind, page, flash={"ok": "revive"})


@router.post("/jobs/dead/{job_id}/bury")
async def bury_action(
    job_id: int,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
    reason: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "",
    page: Annotated[int, Form(ge=1, le=100_000)] = 1,
) -> Response:
    """dead → done + audit с {reason}. Reason — слаг из whitelist (правило 2)."""
    try:
        await bury_dead(
            uow.repositories.jobs,
            uow.repositories.audit,
            AdminId(admin.id),
            JobId(job_id),
            reason.strip(),
            clock.now(),
        )
    except (ConflictError, ValidationError) as exc:
        logger.info(
            "jobs_dead_bury_rejected",
            extra={
                "event": "jobs_dead_bury_rejected",
                "admin_id": admin.id,
                "job_id": job_id,
                "reason": exc.code,
            },
        )
        return _redirect_back(kind, page, flash={"error": exc.code})
    await uow.commit()  # F2: 302 уходит после фиксации bury + audit.
    logger.info(
        "jobs_dead_bury_ok",
        extra={
            "event": "jobs_dead_bury_ok",
            "admin_id": admin.id,
            "job_id": job_id,
            "bury_reason": reason.strip(),
        },
    )
    return _redirect_back(kind, page, flash={"ok": "bury"})
