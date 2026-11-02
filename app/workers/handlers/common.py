"""Общие помощники обработчиков: chat_id Telegram из внешнего id канала."""

from __future__ import annotations

from app.core.jobs.types import TerminalBusinessError


def telegram_chat_id(external_id: str) -> int:
    """Канал Telegram хранит numeric id как текст; некорректный - терминальная ошибка данных."""
    try:
        return int(external_id)
    except ValueError as exc:
        raise TerminalBusinessError("bad_external_id") from exc
