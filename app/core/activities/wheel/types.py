"""Типы колеса момента (ADR-39): призы из wheel_prizes и результат спина."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WheelPrizeRef:
    """Приз колеса из wheel_prizes. Идентичность - id: у призов нет slug (решение U7)."""

    id: int
    activity_id: int
    title: str
    prize_kind: str
    weight: int
    stock_left: int | None
    code_template: str | None


@dataclass(frozen=True)
class WheelSpinResult:
    attempt_id: int
    reward_id: int
    prize_title: str
    prize_kind: str
    code: str | None
