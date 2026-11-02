"""Порт файлов Telegram (ADR-30, п.5 ревью фазы 3c): скачивание по file_id.

Реализация - app/adapters/telegram_files (Bot API getFile и /file/bot<TOKEN>/). Ядро и воркер
видят только байты и исключения порта; токен и httpx остаются в адаптере (правило 4).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class TelegramFileNotFound(Exception):
    """Файл недоступен у Telegram (гость удалил сообщение): ошибка терминальная (канон 04 §4.6)."""


class TelegramFileTooLarge(Exception):
    """Файл больше лимита Telegram Bot API (400 "file is too big"): терминально, file_too_large."""


class TelegramFileTransient(Exception):
    """429, 5xx или сеть: временная ошибка, повтор с backoff."""


@runtime_checkable
class TelegramFilesPort(Protocol):
    async def fetch(self, tg_file_id: str) -> tuple[bytes, str]:
        """Байты файла и content-type, угаданный по пути Telegram. Сверка MIME - по сниффингу."""
        ...
