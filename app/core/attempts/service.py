from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final

from app.common.errors import ConflictError, Forbidden, NotFound, ValidationError
from app.common.time_utils import attempt_date_for
from app.core.attempts.repository import AttemptRepo
from app.core.attempts.state_machine import TERMINAL_STATUSES
from app.core.catalog import CatalogRepo, get_activity_by_slug
from app.core.consent import ConsentRepo, require_consent
from app.core.types.dto import AttemptRef
from app.core.types.enums import ActivityKind, AttemptStatus
from app.core.types.ids import AttemptId, ChannelAccountId, UserId
from app.core.types.results import StartAttemptResult

# 03 §1 attempts: срок жизни `in_progress` по умолчанию, минуты.
DEFAULT_STALE_TTL_MINUTES: Final[int] = 120
_LIMIT_STALE_TTL: Final[str] = "stale_ttl_minutes"


def stale_ttl_for(limits: Mapping[str, Any]) -> timedelta:
    """TTL прохождения: `activities.limits.stale_ttl_minutes`, по умолчанию 120 минут.

    Некорректное значение в справочнике - ValidationError: лучше громко, чем молча
    продлить или укоротить протухание (джоб expire_stale_attempts, 04 §1.3).
    """
    value = limits.get(_LIMIT_STALE_TTL, DEFAULT_STALE_TTL_MINUTES)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError("stale_ttl_invalid")
    return timedelta(minutes=value)


async def start_attempt(
    consents: ConsentRepo,
    catalog: CatalogRepo,
    attempts: AttemptRepo,
    user_id: UserId,
    activity_slug: str,
    channel_account_id: ChannelAccountId,
    now: datetime,
    *,
    expected_kind: ActivityKind | None = None,
    override_expires_at: datetime | None = None,
) -> StartAttemptResult:
    """Начать прохождение: согласие -> активность -> вставка in_progress (08 §4.1).

    «Один раз в день» держит БД (partial unique attempts_once_per_day): вставка
    `ON CONFLICT DO NOTHING`, а не проверка-потом-вставка, поэтому два параллельных
    старта дают ровно одно прохождение, второй получает already_passed_today.
    `expected_kind` - защита от запуска чужого типа активности через общий вход.
    """
    await require_consent(consents, user_id)
    activity = await get_activity_by_slug(catalog, activity_slug)
    if expected_kind is not None and activity.kind != expected_kind:
        raise ValidationError("activity_kind_mismatch")

    attempt = await attempts.insert_in_progress(
        user_id=user_id,
        activity_id=activity.id,
        channel_account_id=channel_account_id,
        started_at=now,
        attempt_date=attempt_date_for(now),
        # 03 §1: срок жизни in_progress; по нему джоб снимает брошенные прохождения.
        # booking (решение координатора, п.4): срок задаёт вызывающий (слот + 10 минут).
        expires_at=(
            override_expires_at
            if override_expires_at is not None
            else now + stale_ttl_for(activity.limits)
        ),
    )
    if attempt is None:
        raise ConflictError("already_passed_today")
    return StartAttemptResult(
        attempt_id=attempt.id,
        activity_id=activity.id,
        activity_slug=activity.slug,
        kind=activity.kind,
        status=attempt.status,
        attempt_date=attempt.attempt_date,
    )


async def get_attempt(repo: AttemptRepo, attempt_id: AttemptId) -> AttemptRef:
    attempt = await repo.get(attempt_id)
    if attempt is None:
        raise NotFound("attempt_not_found")
    return attempt


async def get_owned_attempt(
    repo: AttemptRepo, attempt_id: AttemptId, user_id: UserId
) -> AttemptRef:
    """Прохождение, если оно принадлежит `user_id`; иначе Forbidden('attempt_not_owned').

    Граница канала: вход несёт канал отправителя, и без этой проверки чужой attempt_id
    принимается (ревью фазы 2, S1). Нет прохождения - NotFound('attempt_not_found').
    """
    attempt = await get_attempt(repo, attempt_id)
    if attempt.user_id != user_id:
        raise Forbidden("attempt_not_owned")
    return attempt


async def close_attempt(
    repo: AttemptRepo,
    attempt_id: AttemptId,
    status: AttemptStatus,
    summary: str | None,
    now: datetime,
) -> AttemptRef:
    """Перевести in_progress в терминальный статус одним атомарным UPDATE.

    0 строк -> прохождения нет (NotFound) или его уже закрыл кто-то другой
    (ConflictError('already_closed')): повторный клик/второй воркер ничего не ломает.
    `summary` - одна строка для админки, только слаги и коды, без ПДн.
    """
    if status not in TERMINAL_STATUSES:
        raise ValidationError("status_not_terminal")
    closed = await repo.close_in_progress(attempt_id, status, summary, now)
    if closed is not None:
        return closed
    if await repo.get(attempt_id) is None:
        raise NotFound("attempt_not_found")
    raise ConflictError("already_closed")


async def mark_pending_moderation(repo: AttemptRepo, attempt_id: AttemptId) -> AttemptRef:
    """Прохождение уходит на модерацию: in_progress -> pending_moderation (ADR-16, 02 §6).

    Переход только из in_progress: второе фото на уже идущей модерации - 409, а не вторая
    строка прохождения. Атомарный UPDATE ... WHERE status='in_progress'.
    """
    marked = await repo.set_pending_moderation(attempt_id)
    if marked is not None:
        return marked
    if await repo.get(attempt_id) is None:
        raise NotFound("attempt_not_found")
    raise ConflictError("attempt_not_in_progress")


_MODERATION_CLOSE_STATUSES: Final[frozenset[AttemptStatus]] = frozenset({"completed", "rejected"})


async def close_from_moderation(
    repo: AttemptRepo,
    attempt_id: AttemptId,
    status: AttemptStatus,
    summary: str | None,
    now: datetime,
) -> AttemptRef:
    """Закрыть прохождение решением модерации: pending_moderation -> completed | rejected.

    Атомарный UPDATE ... WHERE status='pending_moderation'. 0 строк - прохождения нет или
    оно уже закрыто (ConflictError), повторное решение ничего не ломает.
    """
    if status not in _MODERATION_CLOSE_STATUSES:
        raise ValidationError("status_not_moderation_close")
    closed = await repo.close_pending_moderation(attempt_id, status, summary, now)
    if closed is not None:
        return closed
    if await repo.get(attempt_id) is None:
        raise NotFound("attempt_not_found")
    raise ConflictError("attempt_not_pending_moderation")


async def cancel_from_moderation(
    repo: AttemptRepo,
    attempt_id: AttemptId,
    reason: str,
    now: datetime,
) -> AttemptRef:
    """Отмена прохождения на модерации: pending_moderation -> cancelled (N3).

    `cancelled` не входит в attempts_once_per_day (03 §2 Q3), поэтому гость может начать
    активность заново в тот же день. Атомарный UPDATE ... WHERE status='pending_moderation'.
    """
    if not reason.strip():
        raise ValidationError("reason_required")
    cancelled = await repo.cancel_pending_moderation(attempt_id, reason, now)
    if cancelled is not None:
        return cancelled
    if await repo.get(attempt_id) is None:
        raise NotFound("attempt_not_found")
    raise ConflictError("attempt_not_pending_moderation")
