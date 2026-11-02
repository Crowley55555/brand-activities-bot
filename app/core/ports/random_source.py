"""Порт случайного выбора для колеса момента (ADR-39).

Реализация - app/adapters/random/system.py; в тестах - детерминированная фикстура
tests/factories/random.py. Ядро не знает, откуда берётся случайность.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class RandomPort(Protocol):
    def choice[T](self, items: Sequence[T], weights: Sequence[int]) -> T:
        """Один элемент с целыми весами (> 0). Пустой список или разная длина - ошибка."""
        ...
