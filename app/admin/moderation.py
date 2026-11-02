"""Экран модерации UGC /admin/ugc (ADR-24, фаза 3b).

Очередь pending, карточка с превью, одобрение и отказ с причиной из данных бренда. Тонкий слой
(правило 3): читает параметры и форму, вызывает core.moderation_queue и core.activities.ugc,
отвечает HTML или 302 на очередь (Q7: в 3b без HTMX). Права на бренд проверяет ядро (RBAC), и
перед каждым решением - тоже. CSRF - app-level csrf_guard (ADR-22); здесь только токен для форм.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from app.admin.templating import render_page
from app.api.deps import current_admin, get_clock, get_uow
from app.api.middleware.csrf import attach_csrf_cookie, current_csrf_value
from app.common.errors import ValidationError
from app.config import Settings, get_settings
from app.core.activities.confirmation import approve as confirmation_approve
from app.core.activities.confirmation import reject as confirmation_reject
from app.core.activities.ugc import approve, reject
from app.core.media import preview_url
from app.core.moderation_queue import (
    PendingUGCRow,
    UGCSubmissionDetail,
    get_submission_detail,
    list_pending_for_brand,
)
from app.core.ports.clock import ClockPort
from app.core.types.dto import AdminRef
from app.core.types.ids import AdminId, UgcSubmissionId
from app.db.unit_of_work import UnitOfWork

router = APIRouter()
logger = logging.getLogger("app.admin.moderation")

QUEUE_PATH: Final[str] = "/admin/ugc"
_TIME_FORMAT: Final[str] = "%d.%m.%Y %H:%M"


@dataclass(frozen=True)
class QueueItem:
    """Строка очереди для шаблона: без внутренних полей и без ПДн."""

    submission_id: int
    brand_name: str
    post_url: str | None
    subscription_url: str | None
    submitted_label: str
    preview_url: str | None


@dataclass(frozen=True)
class DetailItem:
    """Карточка решения для шаблона."""

    submission_id: int
    status: str
    brand_name: str
    activity_name: str
    submitted_label: str
    preview_url: str | None
    post_url: str | None
    subscription_url: str | None
    reject_reasons: Mapping[str, str]


def _preview(submission_id: int, hmac_key: str) -> str:
    """Ссылка на превью; без ключа подписи страница работает, превью не выводим (замечание f)."""
    try:
        return preview_url(hmac_key, submission_id)
    except RuntimeError:
        # Только событие и внутренний id: значения ключа в журнал не попадают.
        logger.warning(
            "preview_unavailable",
            extra={"event": "preview_unavailable", "submission_id": submission_id},
        )
        return ""


def _queue_item(row: PendingUGCRow, hmac_key: str) -> QueueItem:
    return QueueItem(
        submission_id=int(row.submission_id),
        brand_name=row.brand_name,
        post_url=row.post_url,
        subscription_url=row.subscription_url,
        submitted_label=row.submitted_at.strftime(_TIME_FORMAT),
        # Подарок за пост (B-3) - без файла: превью не выводим, работает ссылка на пост.
        preview_url=_preview(int(row.submission_id), hmac_key) if row.file_key else "",
    )


def _detail_item(card: UGCSubmissionDetail, hmac_key: str) -> DetailItem:
    preview = ""
    if card.file_bucket and card.file_key:
        preview = _preview(int(card.submission_id), hmac_key)
    return DetailItem(
        submission_id=int(card.submission_id),
        status=card.status,
        brand_name=card.brand_name,
        activity_name=card.activity_name,
        submitted_label=card.submitted_at.strftime(_TIME_FORMAT),
        preview_url=preview,
        post_url=card.post_url,
        subscription_url=card.subscription_url,
        reject_reasons=card.reject_reasons,
    )


def _form_page(
    request: Request,
    settings: Settings,
    name: str,
    *,
    title: str,
    admin: AdminRef,
    **context: object,
) -> Response:
    """Страница с формами: выдаёт CSRF-токен и ставит cookie, если её ещё не было."""
    csrf_value, is_new = current_csrf_value(request)
    response = render_page(name, title=title, admin=admin, csrf=csrf_value, **context)
    if is_new:
        attach_csrf_cookie(response, csrf_value, secure=settings.cookie_secure)
    return response


@router.get("/ugc")
async def queue(
    request: Request,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    rows = await list_pending_for_brand(uow.repositories.moderation, AdminId(admin.id))
    hmac_key = settings.internal_api_auth_secret
    # CSRF-токен нужен nav.html (форма POST /admin/logout, I-3 фазы 10b-2).
    csrf_value, is_new = current_csrf_value(request)
    response = render_page(
        "moderation.html",
        title="Модерация UGC",
        admin=admin,
        csrf=csrf_value,
        rows=[_queue_item(row, hmac_key) for row in rows],
    )
    if is_new:
        attach_csrf_cookie(response, csrf_value, secure=settings.cookie_secure)
    return response


@router.get("/ugc/{submission_id}")
async def detail(
    submission_id: int,
    request: Request,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    card = await get_submission_detail(
        uow.repositories.moderation, UgcSubmissionId(submission_id), AdminId(admin.id)
    )
    return _form_page(
        request,
        settings,
        "moderation_detail.html",
        title=f"Фото #{submission_id}",
        admin=admin,
        item=_detail_item(card, settings.internal_api_auth_secret),
    )


@router.post("/ugc/{submission_id}/approve")
async def approve_submission(
    submission_id: int,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> Response:
    sid = UgcSubmissionId(submission_id)
    aid = AdminId(admin.id)
    # RBAC до любой записи (E1): чужой бренд не решается даже с верным CSRF.
    card = await get_submission_detail(uow.repositories.moderation, sid, aid)
    r = uow.repositories
    # B-3: один экран очереди, решение - ядро по виду активности (ugc | confirmation).
    if card.activity_kind == "ugc":
        await approve(r.ugc, r.attempts, r.catalog, r.rewards, r.jobs, sid, aid, clock.now())
    elif card.activity_kind == "confirmation":
        await confirmation_approve(
            r.attempts, r.catalog, r.rewards, r.ugc, r.jobs, sid, aid, clock.now()
        )
    else:
        raise ValidationError("unsupported_kind")
    await uow.commit()  # F2: 302 уходит только после фиксации решения, наград и задачи
    return RedirectResponse(QUEUE_PATH, status_code=302)


@router.post("/ugc/{submission_id}/reject")
async def reject_submission(
    submission_id: int,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
    reason_slug: Annotated[str, Form()] = "",
    reason_note: Annotated[str, Form()] = "",
) -> Response:
    sid = UgcSubmissionId(submission_id)
    aid = AdminId(admin.id)
    card = await get_submission_detail(uow.repositories.moderation, sid, aid)
    note = reason_note.strip() or None
    r = uow.repositories
    # B-3: отказ подарка за пост закрывает прохождение сразу, примечание модератора не хранит.
    if card.activity_kind == "ugc":
        await reject(
            r.ugc, r.attempts, r.catalog, r.jobs, sid, aid, reason_slug, note, clock.now()
        )
    elif card.activity_kind == "confirmation":
        await confirmation_reject(
            r.attempts, r.catalog, r.ugc, r.jobs, sid, aid, reason_slug, clock.now()
        )
    else:
        raise ValidationError("unsupported_kind")
    await uow.commit()  # F2: 302 уходит только после фиксации отказа и задачи уведомления
    return RedirectResponse(QUEUE_PATH, status_code=302)
