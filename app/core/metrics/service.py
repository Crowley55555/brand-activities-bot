"""Метрики за день для админки (фаза 7c)."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from app.common.errors import ValidationError
from app.core.metrics.repository import MetricsRepo
from app.core.metrics.types import DailyMetricRow

MIN_DAYS: Final[int] = 1
MAX_DAYS: Final[int] = 90


async def daily_metrics(
    metrics: MetricsRepo,
    days: int,
    brand_slug: str | None,
    now: datetime,
) -> list[DailyMetricRow]:
    if days < MIN_DAYS or days > MAX_DAYS:
        raise ValidationError("days_out_of_range")
    return await metrics.daily_counts(days, brand_slug or None, now)
