"""Порт определения типа файла по байтам (ADR-23, фаза 3b; вызов - в воркере фазы 3c).

Ядро знает только этот интерфейс (правила 3 и 4). Реализация живёт в `app/adapters/file_sniffer/`
(python-magic). Декларацию MIME из Telegram проверяет ядро на приёмке (Q1); реальные байты
сверяет воркер через этот порт и при расхождении ставит ingest_failed с mime_mismatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class MimeInfo:
    """Результат определения: MIME по содержимому и признак изображения."""

    mime: str
    is_image: bool


@runtime_checkable
class FileSnifferPort(Protocol):
    def sniff(self, data: bytes) -> MimeInfo:
        """MIME по содержимому. Неизвестный формат - application/octet-stream, не исключение."""
        ...
