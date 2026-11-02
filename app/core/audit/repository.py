"""Порт журнала аудита (канон 03 §1 audit_log, 07 §2.3): только добавление строк."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from app.core.types.ids import AdminId


class AuditRepo(Protocol):
    async def insert(
        self,
        actor_admin_id: AdminId,
        action: str,
        object_type: str,
        object_id: int,
        details: Mapping[str, Any] | None,
        occurred_at: datetime,
    ) -> None:
        """Добавить строку журнала. Таблица append-only; object_type - из CHECK таблицы."""
        ...
