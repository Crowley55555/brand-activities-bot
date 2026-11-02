from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.metrics.types import DailyMetricRow


@runtime_checkable
class MetricsRepo(Protocol):
    async def daily_counts(
        self, days: int, brand_slug: str | None, now: datetime
    ) -> list[DailyMetricRow]:
        """Строки за `days` суток, заканчивая днём `now` (Europe/Moscow), новые сверху.

        Дни без событий присутствуют с нулями. brand_slug сужает попытки и награды;
        регистрации к бренду не привязаны, поэтому при фильтре по бренду они равны 0.
        """
        ...
