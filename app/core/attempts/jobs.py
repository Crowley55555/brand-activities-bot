"""Джобы прохождений (фаза 10a): expire_stale_attempts и expire_pending_moderation.

Канон 04 §1.3: раз в минуту `scheduler_tick` сканирует просроченные прохождения.

- `expire_stale_attempts` закрывает забытое `in_progress` (TTL `stale_ttl_minutes`,
  поле `attempts.expires_at`) в `expired`. Без этого джоба `attempts_once_per_day`
  держит висячую запись и блокирует повторный старт.
- `expire_pending_moderation` закрывает пропущенное `pending_moderation` в
  `cancelled` (TTL `activities.limits.pending_moderation_ttl_minutes`, миграция 0017).
  Для каждой закрытой строки ставит `notify_ingest_failed` с системной причиной
  `pending_moderation_expired` (миграция 0017 добавляет её в
  `system_reject_reasons` UGC-активностей).

Правило 1 (consent-gate): джобы не трогают ПДн, вызываются из scheduler-тика без
user-контекста. Правило 2 (ПДн в логах): логирование только `event` и `count` —
обеспечивается на уровне scheduler_tick.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from app.core.attempts.repository import AttemptRepo
from app.core.catalog.repository import CatalogRepo
from app.core.jobs.repository import JobRepo
from app.core.jobs.service import enqueue
from app.core.types.dto import ActivityRef
from app.core.types.ids import ActivityId

# Канон 03 §1 attempts: ключ TTL для джоба pending_moderation в activities.limits.
_LIMIT_PENDING_TTL: Final[str] = "pending_moderation_ttl_minutes"

# Слаг причины для notify_ingest_failed; подпись живёт в
# `activities.limits.system_reject_reasons` (миграция 0017 добавляет её UGC-активностям).
PENDING_MODERATION_EXPIRED_REASON: Final[str] = "pending_moderation_expired"


async def expire_stale_attempts(
    repo: AttemptRepo, now: datetime, limit: int = 500
) -> int:
    """Закрыть просроченные in_progress пачкой до `limit` штук. Возвращает число закрытых."""
    expired = await repo.expire_due(now, limit)
    return len(expired)


def _pending_ttl_minutes(limits: Mapping[str, Any]) -> int | None:
    """Чтение `pending_moderation_ttl_minutes` из limits.

    Нет ключа — активность не подпадает под джоб (fail-closed, канон 03 §382).
    Невалидное значение (не int или <=0) — None и логирование у вызывающего:
    молча закрывать десятки записей на кривой конфиге нельзя.
    """
    value = limits.get(_LIMIT_PENDING_TTL)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _activities_with_pending_ttl(
    activities: Sequence[ActivityRef],
) -> list[tuple[ActivityId, int]]:
    """(activity_id, ttl_minutes) для активностей с валидным TTL. Пустой список — ok."""
    pairs: list[tuple[ActivityId, int]] = []
    for activity in activities:
        ttl = _pending_ttl_minutes(activity.limits)
        if ttl is not None:
            pairs.append((activity.id, ttl))
    return pairs


async def expire_pending_moderation(
    attempts: AttemptRepo,
    catalog: CatalogRepo,
    jobs: JobRepo,
    activities: Sequence[ActivityRef],
    now: datetime,
    limit: int = 500,
) -> int:
    """Закрыть пропущенные pending_moderation в cancelled; для каждого — notify_ingest_failed.

    На вход — активности бренда/каталога, чтобы джоб не ходил в каталог сам
    (строгая зависимость catalog -> attempts остаётся в вызывающем). Для каждой
    закрытой строки ставим системное уведомление: гость получит технический текст,
    а не «работа отклонена». `attempt_closed=True`: cancelled — терминал.
    Фаза 10b-3, N-1: `limit` на одну активность берётся из EXPIRE_BATCH_SIZE.
    """
    pairs = _activities_with_pending_ttl(activities)
    if not pairs:
        return 0
    rows = await attempts.expire_pending_moderation_due(now, pairs, limit=limit)
    for attempt_id, activity_id, user_id in rows:
        activity = _find_activity(activities, activity_id)
        if activity is None:
            # Активность исчезла между SELECT и UPDATE — пропускаем уведомление,
            # но строка уже закрыта (терминал, повторного notify не будет).
            continue
        # catalog нужен подписчику enqueue только для чтения slug; здесь он уже
        # у ActivityRef. Передаём минимальный payload — такой же, как у ingest_failed.
        await enqueue(
            jobs,
            "notify_ingest_failed",
            {
                "user_id": int(user_id),
                "attempt_id": int(attempt_id),
                "system_reason_slug": PENDING_MODERATION_EXPIRED_REASON,
                "attempt_closed": True,
                "activity_slug": activity.slug,
            },
            run_after=now,
            idempotency_key=f"pending_moderation_expired:{int(attempt_id)}",
        )
    # catalog не трогается напрямую, но в сигнатуре он есть — нужен для контракта
    # и будущих расширений (ревью может попросить подтянуть ActivityRef вживую).
    _ = catalog
    return len(rows)


def _find_activity(
    activities: Sequence[ActivityRef], activity_id: ActivityId
) -> ActivityRef | None:
    for activity in activities:
        if activity.id == activity_id:
            return activity
    return None
