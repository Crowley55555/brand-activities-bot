"""Адаптер файлов Telegram (ADR-30, п.5 ревью): реализация порта TelegramFilesPort."""

from __future__ import annotations

import httpx

from app.adapters.telegram_files.telegram_api import TelegramApiFiles
from app.config import Settings
from app.core.ports.telegram_files import TelegramFilesPort


def build(settings: Settings, client: httpx.AsyncClient) -> TelegramFilesPort:
    return TelegramApiFiles(bot_cred=settings.telegram_bot_token, client=client)


__all__ = ["build"]
