"""Журнал действий администратора (фаза 4): запись о приходе, no_show и т. п.

Пишем для appointments и users (reveal_user_phone, фаза 7a): CHECK audit_log.object_type не
содержит slots и activities (0001). Расширение списка - отдельная миграция.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.core.audit.repository import AuditRepo
from app.core.types.ids import AdminId


async def log_admin_action(
    repo: AuditRepo,
    admin_id: AdminId,
    action: str,
    object_type: str,
    object_id: int,
    occurred_at: datetime,
    details: Mapping[str, Any] | None = None,
) -> None:
    await repo.insert(admin_id, action, object_type, object_id, details, occurred_at)
