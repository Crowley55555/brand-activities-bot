"""RandomPort на системном генераторе (ADR-39): secrets.SystemRandom, без seed в продакшене."""

from __future__ import annotations

import secrets
from collections.abc import Sequence


class SystemRandomPort:
    def __init__(self) -> None:
        self._rng = secrets.SystemRandom()

    def choice[T](self, items: Sequence[T], weights: Sequence[int]) -> T:
        if not items or len(items) != len(weights) or any(weight <= 0 for weight in weights):
            raise ValueError("random_choice_invalid")
        return self._rng.choices(list(items), weights=list(weights), k=1)[0]
