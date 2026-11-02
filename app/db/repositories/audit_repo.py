"""SQL-реализация журнала аудита (порт core.audit.AuditRepo): только INSERT."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types.ids import AdminId


class SqlAuditRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(
        self,
        actor_admin_id: AdminId,
        action: str,
        object_type: str,
        object_id: int,
        details: Mapping[str, Any] | None,
        occurred_at: datetime,
    ) -> None:
        await self._session.execute(
            text(
                "INSERT INTO audit_log (actor_admin_id, action, object_type, object_id, "
                "details, occurred_at, created_at) "
                "VALUES (:admin, :action, :otype, :oid, CAST(:details AS jsonb), :at, now())"
            ),
            {
                "admin": int(actor_admin_id),
                "action": action,
                "otype": object_type,
                "oid": object_id,
                "details": None if details is None else json.dumps(dict(details)),
                "at": occurred_at,
            },
        )
