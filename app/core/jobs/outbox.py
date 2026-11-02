"""Outbox telegram_send (ADR-31, ADR-32): claim с рендером текста и ack результатов отправки.

Воркер ставит задачу с template и context (ids и причина, без кода). Текст собирается здесь, при
claim: tg_worker получает готовый text, а код промокода в payload задачи не лежит (решение п.8
ревью фазы 3c; журнал 3b: код не кладём в payload). Тексты приходят из конфига (правило 5).

Три шаблона (ТОП-1 ревью фазы 3c):
- reward_notice - награды Бренд B (voucher и promocode по id);
- ugc_rejected - отказ модератора: подпись причины из activities.limits.reject_reasons;
- ingest_failed - системный сбой приёмки: подпись из activities.limits.system_reject_reasons
  (миграция 0011). Текст - про техническую неудачу, а не про «работа отклонена».
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from app.common.errors import ConflictError, NotFound
from app.common.time_utils import EVENT_TIMEZONE
from app.core.catalog.repository import CatalogRepo
from app.core.channels import mark_unlinked
from app.core.channels.repository import ChannelAccountRepo
from app.core.jobs.repository import JobRepo
from app.core.jobs.service import (
    ack_dead,
    ack_retryable,
    ack_success,
    ack_terminal_business,
    claim_batch,
)
from app.core.jobs.types import JobRecord, OutboxResult
from app.core.types.dto import RewardIssued
from app.core.types.ids import JobId

logger = logging.getLogger("app.core.jobs.outbox")

TELEGRAM_SEND: Final[str] = "telegram_send"
TEMPLATE_REWARD_NOTICE: Final[str] = "reward_notice"
TEMPLATE_UGC_REJECTED: Final[str] = "ugc_rejected"
TEMPLATE_INGEST_FAILED: Final[str] = "ingest_failed"
TEMPLATE_APPOINTMENT_REMINDER: Final[str] = "appointment_reminder"
TEMPLATE_APPOINTMENT_AUTO_CANCELLED: Final[str] = "appointment_auto_cancelled"
TEMPLATE_APPOINTMENT_LEAFLET: Final[str] = "appointment_leaflet"
# Фаза 5 (ADR-39, ADR-40, ADR-42): призы колеса, результат рамки, промокод и отказ по ссылке.
TEMPLATE_WHEEL_RESULT: Final[str] = "wheel_result"
TEMPLATE_POLAROID_READY: Final[str] = "polaroid_ready"
TEMPLATE_PROMOCODE: Final[str] = "promocode"
TEMPLATE_CONFIRMATION_REJECTED: Final[str] = "confirmation_rejected"
_APPOINTMENT_TEMPLATES: Final[frozenset[str]] = frozenset(
    {
        TEMPLATE_APPOINTMENT_REMINDER,
        TEMPLATE_APPOINTMENT_AUTO_CANCELLED,
        TEMPLATE_APPOINTMENT_LEAFLET,
    }
)
RENDER_FAILED: Final[str] = "render_failed"
_BLOCKED_BY_USER: Final[str] = "user_blocked"
_FENCING: Final[str] = "fencing_failed"


@dataclass(frozen=True)
class OutboxTexts:
    """Шаблоны текстов из конфига: плейсхолдеры str.format (правило 5)."""

    reward_notice: str
    ugc_rejected_retry: str
    ugc_rejected_closed: str
    ingest_failed_retry: str
    ingest_failed_closed: str
    # Фаза 4: пустые значения по умолчанию - тексты приходят из конфига (правило 5).
    appointment_reminder: str = ""
    appointment_auto_cancelled: str = ""
    appointment_leaflet: str = ""
    public_base_url: str = ""
    # Фаза 5: тексты из конфига (правило 5); плейсхолдеры - {prize}, {title}/{code}, {link},
    # {reason}.
    wheel_result: str = ""
    polaroid_ready: str = ""
    promocode: str = ""
    confirmation_rejected: str = ""


@dataclass(frozen=True)
class OutboxMessage:
    """Готовое к отправке сообщение: то, что получает tg_worker (ответ /outbox/claim)."""

    job_id: JobId
    chat_id: int
    text: str
    # M-1 (фаза 5): абсолютный URL фото (/media/frame/...). Задан - tg_worker шлёт фото по ссылке
    # (send_photo, Telegram скачивает файл сам), иначе - текст (send_message).
    media_url: str | None = None


@runtime_checkable
class OutboxRewards(Protocol):
    async def list_by_ids(self, reward_ids: Sequence[int]) -> list[RewardIssued]: ...


@dataclass(frozen=True)
class AppointmentText:
    """То, что нужно тексту записи: название активности, слот и настройки (без ПДн)."""

    activity_name: str
    slot_start: datetime
    limits: Mapping[str, Any]


@runtime_checkable
class OutboxAppointments(Protocol):
    async def describe(self, appointment_id: int) -> AppointmentText | None: ...


class _RenderFailure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _label(limits: Mapping[str, Any], dictionary: str, reason_slug: str) -> str:
    """Подпись причины из словаря activities.limits; неизвестный слаг - ошибка рендера."""
    reasons = limits.get(dictionary)
    if not isinstance(reasons, Mapping) or reason_slug not in reasons:
        raise _RenderFailure("reason_unknown")
    label = reasons[reason_slug]
    if not isinstance(label, str):
        raise _RenderFailure("reason_unknown")
    return label


def _reason_context(context: Mapping[str, Any]) -> tuple[str, str, bool]:
    reason_slug = str(context.get("reason_slug", ""))
    activity_slug = str(context.get("activity_slug", ""))
    closed = context.get("attempt_closed")
    if not isinstance(closed, bool) or not reason_slug or not activity_slug:
        raise _RenderFailure("payload_invalid")
    return reason_slug, activity_slug, closed


async def _render_appointment(
    template: str,
    context: Mapping[str, Any],
    appointments: OutboxAppointments,
    texts: OutboxTexts,
) -> str:
    """Тексты booking: время слота - московское, минуты и листовка - из activities.limits."""
    appointment_id = context.get("appointment_id")
    if isinstance(appointment_id, bool) or not isinstance(appointment_id, int):
        raise _RenderFailure("payload_invalid")
    info = await appointments.describe(appointment_id)
    if info is None:
        raise _RenderFailure("appointment_missing")
    local_time = info.slot_start.astimezone(ZoneInfo(EVENT_TIMEZONE)).strftime("%H:%M")
    if template == TEMPLATE_APPOINTMENT_LEAFLET:
        leaflet_url = str(info.limits.get("leaflet_url", ""))
        if not leaflet_url:
            raise _RenderFailure("leaflet_missing")
        url = texts.public_base_url.rstrip("/") + leaflet_url
        return texts.appointment_leaflet.format(
            title=str(info.limits.get("leaflet_title", "")), url=url
        )
    if template == TEMPLATE_APPOINTMENT_REMINDER:
        minutes = int(info.limits.get("remind_before_minutes", 5))
        return texts.appointment_reminder.format(
            minutes=minutes, activity=info.activity_name, time=local_time
        )
    return texts.appointment_auto_cancelled.format(activity=info.activity_name, time=local_time)


async def _render_wheel_or_promocode(
    template: str,
    context: Mapping[str, Any],
    rewards: OutboxRewards,
    texts: OutboxTexts,
) -> str:
    """Приз колеса (wheel_result) и промокод (promocode): один reward по id, текст из конфига."""
    raw_ids = context.get("reward_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise _RenderFailure("payload_invalid")
    issued = await rewards.list_by_ids([int(item) for item in raw_ids])
    if not issued:
        raise _RenderFailure("reward_missing")
    reward = issued[0]
    if template == TEMPLATE_PROMOCODE:
        if reward.reward_type != "promocode" or not reward.code:
            raise _RenderFailure("reward_missing")
        return texts.promocode.format(title=reward.title or "", code=reward.code)
    return texts.wheel_result.format(prize=reward.title or "")


def _render_polaroid(
    context: Mapping[str, Any],
    texts: OutboxTexts,
    frame_link: Callable[[int], str] | None,
) -> tuple[str, str]:
    """Polaroid (ADR-40, M-1): подпись и абсолютный URL фото; Telegram скачивает фото сам.

    Относительная ссылка на результат - от API (frame_link), абсолютный адрес - PUBLIC_BASE_URL.
    """
    frame_id = context.get("frame_submission_id")
    if isinstance(frame_id, bool) or not isinstance(frame_id, int):
        raise _RenderFailure("payload_invalid")
    if frame_link is None:
        raise _RenderFailure("link_unavailable")
    media_url = texts.public_base_url.rstrip("/") + frame_link(frame_id)
    return texts.polaroid_ready.format(link=media_url), media_url


async def _render(
    job: JobRecord,
    rewards: OutboxRewards,
    catalog: CatalogRepo,
    texts: OutboxTexts,
    appointments: OutboxAppointments | None = None,
    frame_link: Callable[[int], str] | None = None,
) -> str:
    template = str(job.payload.get("template", ""))
    context = job.payload.get("context")
    if not isinstance(context, Mapping):
        raise _RenderFailure("payload_invalid")
    if template in (TEMPLATE_WHEEL_RESULT, TEMPLATE_PROMOCODE):
        return await _render_wheel_or_promocode(template, context, rewards, texts)
    if template == TEMPLATE_CONFIRMATION_REJECTED:
        reason_slug, activity_slug, _closed = _reason_context(context)
        activity = await catalog.find_activity_by_slug(activity_slug)
        if activity is None:
            raise _RenderFailure("activity_missing")
        label = _label(activity.limits, "reject_reasons", reason_slug)
        return texts.confirmation_rejected.format(reason=label)
    if template == TEMPLATE_REWARD_NOTICE:
        raw_ids = context.get("reward_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise _RenderFailure("payload_invalid")
        issued = await rewards.list_by_ids([int(item) for item in raw_ids])
        promo = next((r for r in issued if r.reward_type == "promocode" and r.code), None)
        voucher = next((r for r in issued if r.reward_type == "voucher" and r.code), None)
        if promo is None or voucher is None:
            raise _RenderFailure("reward_missing")
        return texts.reward_notice.format(
            promocode=promo.code,
            voucher_title=voucher.title or "",
            voucher_code=voucher.code,
        )
    if template in (TEMPLATE_UGC_REJECTED, TEMPLATE_INGEST_FAILED):
        reason_slug, activity_slug, closed = _reason_context(context)
        activity = await catalog.find_activity_by_slug(activity_slug)
        if activity is None:
            raise _RenderFailure("activity_missing")
        if template == TEMPLATE_UGC_REJECTED:
            label = _label(activity.limits, "reject_reasons", reason_slug)
            pattern = texts.ugc_rejected_closed if closed else texts.ugc_rejected_retry
        else:
            label = _label(activity.limits, "system_reject_reasons", reason_slug)
            pattern = texts.ingest_failed_closed if closed else texts.ingest_failed_retry
        return pattern.format(reason=label)
    if template in _APPOINTMENT_TEMPLATES:
        if appointments is None:
            raise _RenderFailure("template_unknown")
        return await _render_appointment(template, context, appointments, texts)
    raise _RenderFailure("template_unknown")


async def claim_outbox(
    jobs: JobRepo,
    rewards: OutboxRewards,
    catalog: CatalogRepo,
    texts: OutboxTexts,
    *,
    worker_id: str,
    limit: int,
    visibility_timeout_s: int,
    appointments: OutboxAppointments | None = None,
    frame_link: Callable[[int], str] | None = None,
) -> list[OutboxMessage]:
    """Claim telegram_send и рендер текста. Задача, которую не отрендерить, - терминальная.

    Терминальность здесь - внутри транзакции claim: повторно такая задача не уйдёт в сеть.
    frame_link (фаза 5) - относительная ссылка на результат Polaroid; абсолютный адрес -
    PUBLIC_BASE_URL.
    """
    records = await claim_batch(jobs, worker_id, [TELEGRAM_SEND], limit, visibility_timeout_s)
    messages: list[OutboxMessage] = []
    for record in records:
        try:
            chat_id = int(record.payload["chat_id"])
            media_url: str | None = None
            if record.payload.get("template") == TEMPLATE_POLAROID_READY:
                polaroid_context = record.payload.get("context")
                if not isinstance(polaroid_context, Mapping):
                    raise _RenderFailure("payload_invalid")
                body, media_url = _render_polaroid(polaroid_context, texts, frame_link)
            else:
                body = await _render(record, rewards, catalog, texts, appointments, frame_link)
        except (_RenderFailure, KeyError, TypeError, ValueError) as exc:
            reason = exc.reason if isinstance(exc, _RenderFailure) else "payload_invalid"
            await ack_terminal_business(jobs, record.id, worker_id, RENDER_FAILED)
            logger.warning(
                "outbox_render_failed",
                extra={
                    "event": "outbox_render_failed",
                    "job_id": int(record.id),
                    "reason": reason,
                },
            )
            continue
        messages.append(
            OutboxMessage(job_id=record.id, chat_id=chat_id, text=body, media_url=media_url)
        )
    return messages


async def ack_outbox(
    jobs: JobRepo,
    channels: ChannelAccountRepo,
    job_id: JobId,
    worker_id: str,
    result: OutboxResult,
    error: str | None,
    retry_after_s: int | None,
    now: datetime,
) -> None:
    """Исход отправки из tg_worker. Fencing: чужой или снятый замок - 409 fencing_failed.

    Блок бота (user_blocked) помечает канал unlinked_at (п.9 ревью 3c) - только после успешного ack.
    """
    record = await jobs.get(job_id, for_update=True)
    if record is None or record.kind != TELEGRAM_SEND:
        raise NotFound("job_not_found")
    if record.status != "locked":
        raise ConflictError(_FENCING)
    if result == "success":
        await ack_success(jobs, job_id, worker_id)
    elif result == "retryable":
        await ack_retryable(jobs, job_id, worker_id, error or "telegram_unavailable", retry_after_s)
    elif result == "terminal_business":
        slug = error or "unknown"
        await ack_terminal_business(jobs, job_id, worker_id, slug)
        if slug == _BLOCKED_BY_USER:
            await mark_unlinked(channels, "telegram", str(record.payload["chat_id"]), now)
    else:
        await ack_dead(jobs, job_id, worker_id, error or "outbox_dead")
