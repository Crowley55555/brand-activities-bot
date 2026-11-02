from __future__ import annotations

from typing import Final

from app.core.types.enums import AttemptStatus

# Статусы, из которых прохождение больше не выходит (02 §6, 03 attempts.status).
TERMINAL_STATUSES: Final[frozenset[AttemptStatus]] = frozenset(
    {"completed", "approved", "rejected", "cancelled", "expired"}
)

# Прохождение «в работе»: гость или модератор ещё могут продвинуть его дальше.
IN_PROGRESS_LIKE_STATUSES: Final[frozenset[AttemptStatus]] = frozenset(
    {"in_progress", "pending_moderation"}
)


def is_terminal(status: AttemptStatus) -> bool:
    return status in TERMINAL_STATUSES
