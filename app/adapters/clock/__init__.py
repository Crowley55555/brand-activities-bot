from __future__ import annotations

from app.adapters.clock.system import SystemClock
from app.core.ports.clock import ClockPort


def build() -> ClockPort:
    return SystemClock()


__all__ = ["SystemClock", "build"]
