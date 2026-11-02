"""/forget каскад (фаза 10b-3, I-2): публичный API модуля."""

from __future__ import annotations

from app.core.personal_data.service import (
    ForgetResult,
    confirm_forget,
    request_forget,
)

__all__ = [
    "ForgetResult",
    "confirm_forget",
    "request_forget",
]
