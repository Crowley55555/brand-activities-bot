"""Публичная дверь подарка за пост (08 §3): снаружи ходят только через этот пакет."""

from __future__ import annotations

from app.core.activities.confirmation.service import accept_confirmation, approve, reject

__all__ = ["accept_confirmation", "approve", "reject"]
