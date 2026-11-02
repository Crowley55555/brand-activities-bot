"""DLQ-операции (фаза 10b-2, I-1): list_dead / revive_dead / bury_dead.

Headless-ядро (правило 3): админ-слой не знает про SQL, только про эти три функции.
Каждая state-changing операция пишет строку в `audit_log` (канон 07 §2.3,
миграция 0020 добавляет `jobs` в CHECK `audit_log.object_type`).

Инварианты:
* `revive_dead` возвращает задачу в очередь: `status='queued'`, `attempts=0`,
  `last_error=NULL`, `run_after=now`. Для клаймера это обычная новая задача.
* `bury_dead` закрывает задачу семантически: `status='done'`, `last_error`
  содержит маркер `buried:<slug>`. Повтор не берётся (клаймер смотрит queued).
  Строка остаётся в БД — на неё ссылается `audit_log.object_id`.
* `reason` для bury — только из whitelist `BURY_REASON_SLUGS` (правило 2:
  причина не несёт ПДн и свободный текст); посторонний слаг → `ValidationError`.
* Конкурентный revive/bury: атомарный UPDATE ... WHERE status='dead' RETURNING id;
  rowcount=0 → `ConflictError('not_dead')` (кто-то успел раньше).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from app.common.errors import ConflictError, ValidationError
from app.core.audit.repository import AuditRepo
from app.core.audit.service import log_admin_action
from app.core.jobs.repository import DeadJobRow, JobRepo
from app.core.types.ids import AdminId, JobId

AUDIT_OBJECT_TYPE: Final[str] = "jobs"
ACTION_REVIVE: Final[str] = "jobs_dead_revive"
ACTION_BURY: Final[str] = "jobs_dead_bury"
BURY_MARKER_PREFIX: Final[str] = "buried:"

# Канон 00 правило 2: причина не содержит ПДн и свободный текст. Минимальный
# набор классификаций операции (см. промт §3.5). Константа в коде, не в БД,
# потому что это не бизнес-слог бренда, а операционная классификация.
BURY_REASON_SLUGS: Final[frozenset[str]] = frozenset(
    {
        "manually_delivered",
        "data_invalid",
        "operator_notified",
    }
)


async def list_dead(
    repo: JobRepo, kind: str | None, limit: int, offset: int
) -> tuple[list[DeadJobRow], int]:
    """Страница dead-задач + общее число для пагинации."""
    if limit < 1 or offset < 0:
        raise ValidationError("list_dead_params_invalid")
    rows = await repo.list_dead(kind, limit, offset)
    total = await repo.count_dead(kind)
    return rows, total


async def revive_dead(
    repo: JobRepo,
    audit: AuditRepo,
    admin_id: AdminId,
    job_id: JobId,
    now: datetime,
) -> None:
    """dead → queued (attempts=0, last_error=NULL) + запись в audit_log.

    Если задача уже не dead (кто-то починил/похоронил) — ConflictError('not_dead'),
    аудит не пишется.
    """
    if not await repo.revive_dead(job_id, now):
        raise ConflictError("not_dead")
    await log_admin_action(
        audit, admin_id, ACTION_REVIVE, AUDIT_OBJECT_TYPE, int(job_id), now
    )


async def bury_dead(
    repo: JobRepo,
    audit: AuditRepo,
    admin_id: AdminId,
    job_id: JobId,
    reason: str,
    now: datetime,
) -> None:
    """dead → done, last_error='buried:<reason>' + запись в audit_log.

    `reason` — слаг из `BURY_REASON_SLUGS`; иначе ValidationError('bury_reason_unknown').
    details в audit_log несёт `{"reason": <slug>}` — слаг безопасен для хранения
    (без ПДн, без свободного текста).
    """
    if reason not in BURY_REASON_SLUGS:
        raise ValidationError("bury_reason_unknown")
    marker = f"{BURY_MARKER_PREFIX}{reason}"
    if not await repo.bury_dead(job_id, marker):
        raise ConflictError("not_dead")
    await log_admin_action(
        audit,
        admin_id,
        ACTION_BURY,
        AUDIT_OBJECT_TYPE,
        int(job_id),
        now,
        details={"reason": reason},
    )
