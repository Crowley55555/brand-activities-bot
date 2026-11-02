"""Адаптер случайного выбора (ADR-39): RandomPort на secrets.SystemRandom."""

from __future__ import annotations

from app.adapters.random.system import SystemRandomPort

__all__ = ["SystemRandomPort"]
