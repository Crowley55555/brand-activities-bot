from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.time_utils import EVENT_TIMEZONE
from app.core.metrics.types import DailyMetricRow

# Границы суток считаем в EVENT_TIMEZONE (Europe/Moscow), как attempt_date в схеме.
_SQL = text(
    f"""
WITH days AS (
    SELECT d::date AS day
    FROM generate_series(CAST(:start AS date), CAST(:today AS date), interval '1 day') AS d
),
regs AS (
    -- users не связан с брендом: при фильтре по бренду регистрации не считаем (0)
    SELECT (registered_at AT TIME ZONE '{EVENT_TIMEZONE}')::date AS day, COUNT(*) AS n
    FROM users
    WHERE registered_at IS NOT NULL AND CAST(:slug AS text) IS NULL
    GROUP BY 1
),
done AS (
    SELECT (a.finished_at AT TIME ZONE '{EVENT_TIMEZONE}')::date AS day, COUNT(*) AS n
    FROM attempts a
    JOIN activities act ON act.id = a.activity_id
    JOIN brands b ON b.id = act.brand_id
    WHERE a.status IN ('completed','approved') AND a.finished_at IS NOT NULL
      AND (CAST(:slug AS text) IS NULL OR b.slug = :slug)
    GROUP BY 1
),
rw AS (
    SELECT (r.issued_at AT TIME ZONE '{EVENT_TIMEZONE}')::date AS day,
           COUNT(*) FILTER (WHERE r.reward_type = 'promocode') AS promos,
           COUNT(*) FILTER (WHERE r.reward_type = 'voucher') AS vouchers
    FROM rewards r
    JOIN attempts a ON a.id = r.attempt_id
    JOIN activities act ON act.id = a.activity_id
    JOIN brands b ON b.id = act.brand_id
    WHERE r.reward_type IN ('promocode', 'voucher')
      AND (CAST(:slug AS text) IS NULL OR b.slug = :slug)
    GROUP BY 1
)
SELECT d.day,
       COALESCE(regs.n, 0) AS registrations,
       COALESCE(done.n, 0) AS completed_attempts,
       COALESCE(rw.promos, 0) AS promocodes,
       COALESCE(rw.vouchers, 0) AS vouchers
FROM days d
LEFT JOIN regs ON regs.day = d.day
LEFT JOIN done ON done.day = d.day
LEFT JOIN rw ON rw.day = d.day
ORDER BY d.day DESC
"""
)


class SqlMetricsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def daily_counts(
        self, days: int, brand_slug: str | None, now: datetime
    ) -> list[DailyMetricRow]:
        today = now.astimezone(ZoneInfo(EVENT_TIMEZONE)).date()
        start = today - timedelta(days=days - 1)
        result = await self._session.execute(
            _SQL, {"start": start, "today": today, "slug": brand_slug}
        )
        return [
            DailyMetricRow(
                day=row.day,
                registrations=int(row.registrations),
                completed_attempts=int(row.completed_attempts),
                promocodes_issued=int(row.promocodes),
                vouchers_issued=int(row.vouchers),
            )
            for row in result
        ]
