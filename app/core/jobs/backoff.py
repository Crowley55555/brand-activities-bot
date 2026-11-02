"""Backoff очереди (канон 04 §3.3): min(60 с · 2^(attempts-1), 2 ч), джиттер ±20%.

attempts - номер текущей неудачной попытки (увеличивается при claim). Шесть попыток дают около
31 минуты активных повторов; на шестой неудаче задача уходит в dead (ack_retryable).
"""

from __future__ import annotations

from typing import Final

BASE_DELAY_S: Final[int] = 60
MAX_DELAY_S: Final[int] = 2 * 60 * 60
JITTER_RANGE: Final[float] = 0.2


def backoff_seconds(attempts: int, jitter: float = 1.0) -> float:
    """Задержка перед следующей попыткой. jitter в [0.8, 1.2]; 1.0 - без джиттера (тесты)."""
    if attempts < 1:
        raise ValueError("attempts_must_be_positive")
    base = min(BASE_DELAY_S * (2 ** (attempts - 1)), MAX_DELAY_S)
    return float(base) * jitter
