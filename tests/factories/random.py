"""Детерминированный RandomPort для тестов колеса (ADR-39): одинаковый seed - одинаковый выбор.

Интерфейс совпадает с app/core/ports/random_source.py (RandomPort). В тестах секреты не нужны.
"""

from __future__ import annotations

import random
from collections.abc import Sequence


class SeededRandomPort:
    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def choice[T](self, items: Sequence[T], weights: Sequence[int]) -> T:
        return self._rng.choices(list(items), weights=list(weights), k=1)[0]
