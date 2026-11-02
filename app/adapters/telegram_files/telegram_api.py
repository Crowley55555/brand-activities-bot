"""Telegram Bot API: getFile и скачивание файла по file_path (ADR-30, порт TelegramFilesPort).

Классификация ответов (ревью фазы 3c, пункт G):
- 400 с описанием "file is too big" -> TelegramFileTooLarge (терминально: file_too_large);
- 400 или 404 (файл недоступен, неверный file_id) -> TelegramFileNotFound (file_not_found);
- 429, 5xx, прочие не-200 и сетевые ошибки -> TelegramFileTransient (повтор с backoff).
Токен бота попадает в URL запроса: адаптер не логирует URL и не кладёт его в тексты исключений.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

from app.core.ports.telegram_files import (
    TelegramFileNotFound,
    TelegramFileTooLarge,
    TelegramFileTransient,
)

API_BASE: Final[str] = "https://api.telegram.org"
_MIME_BY_EXTENSION: Final[dict[str, str]] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "pdf": "application/pdf",
}
_NOT_FOUND_STATUSES: Final[frozenset[int]] = frozenset({400, 404})
_TOO_BIG_MARK: Final[str] = "too big"


def _mime_for(file_path: str) -> str:
    extension = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    return _MIME_BY_EXTENSION.get(extension, "application/octet-stream")


def _description(response: httpx.Response) -> str:
    """Поле description ошибки Telegram в нижнем регистре; без JSON - пустая строка."""
    try:
        body: Any = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        return str(body.get("description", "")).lower()
    return ""


class TelegramApiFiles:
    """Реализация TelegramFilesPort поверх httpx (клиент создаёт и закрывает вызывающий)."""

    def __init__(
        self,
        *,
        bot_cred: str,
        client: httpx.AsyncClient,
        api_base: str = API_BASE,
    ) -> None:
        self._bot_cred = bot_cred
        self._client = client
        self._base = api_base.rstrip("/")

    async def fetch(self, tg_file_id: str) -> tuple[bytes, str]:
        file_path = await self._file_path(tg_file_id)
        response = await self._get(f"{self._base}/file/bot{self._bot_cred}/{file_path}")
        return response.content, _mime_for(file_path)

    async def _file_path(self, tg_file_id: str) -> str:
        response = await self._get(
            f"{self._base}/bot{self._bot_cred}/getFile",
            params={"file_id": tg_file_id},
        )
        try:
            body: Any = response.json()
            return str(body["result"]["file_path"])
        except (ValueError, KeyError, TypeError) as exc:
            raise TelegramFileNotFound() from exc

    async def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            response = await self._client.get(url, params=params)
        except httpx.TransportError as exc:
            raise TelegramFileTransient() from exc
        if response.status_code == 200:
            return response
        if response.status_code == 400 and _TOO_BIG_MARK in _description(response):
            raise TelegramFileTooLarge()
        if response.status_code in _NOT_FOUND_STATUSES:
            raise TelegramFileNotFound()
        raise TelegramFileTransient()
