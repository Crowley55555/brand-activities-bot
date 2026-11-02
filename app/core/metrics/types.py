"""DTO метрик за день (фаза 7c): только числа, без ПДн (правило 9)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class DailyMetricRow:
    day: date
    registrations: int
    completed_attempts: int
    promocodes_issued: int
    vouchers_issued: int
