"""Адаптер FileSnifferPort (ADR-23, фаза 3b): python-magic. Сборку вызывает воркер фазы 3c."""

from __future__ import annotations

from app.adapters.file_sniffer.python_magic import PythonMagicSniffer
from app.core.ports.file_sniffer import FileSnifferPort


def build() -> FileSnifferPort:
    return PythonMagicSniffer()


__all__ = ["PythonMagicSniffer", "build"]
