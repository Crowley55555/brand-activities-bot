"""Переходы строки ugc_submissions (04 §5.2).

Повтор реализован как новая строка того же прохождения (resubmit, 02 §13), а не как переход
строки: строка с ingest_failed или rejected остаётся терминальной. ingest_failed не отменяет
прохождение (F4): оно остаётся pending_moderation, сбой считается в max_resubmits.
`can_transition` вызывают approve и reject (ревью F8c).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from app.core.types.enums import UgcStatus

_TRANSITIONS: Final[Mapping[UgcStatus, frozenset[UgcStatus]]] = {
    "receiving": frozenset({"pending", "ingest_failed"}),
    "pending": frozenset({"approved", "rejected"}),
    "ingest_failed": frozenset(),
    "rejected": frozenset(),
    "approved": frozenset(),
}


def can_transition(src: UgcStatus, dst: UgcStatus) -> bool:
    return dst in _TRANSITIONS[src]
