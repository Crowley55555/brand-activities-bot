"""Адаптер TelegramApiFiles (ADR-30, п.5 и пункт G ревью): getFile и скачивание, MockTransport."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.adapters.telegram_files.telegram_api import TelegramApiFiles
from app.core.ports.telegram_files import (
    TelegramFileNotFound,
    TelegramFileTooLarge,
    TelegramFileTransient,
)

CRED = "123456:fake-cred-value-for-tests"


def _adapter(handler: Any) -> TelegramApiFiles:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramApiFiles(bot_cred=CRED, client=client)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/getFile"):
        return httpx.Response(
            200, json={"ok": True, "result": {"file_id": "x", "file_path": "photos/p1.jpg"}}
        )
    if "/file/bot" in request.url.path:
        return httpx.Response(200, content=b"\xff\xd8jpeg-body")
    return httpx.Response(500)


async def test_fetch_downloads_file_and_guesses_mime() -> None:
    data, mime = await _adapter(_ok_handler).fetch("file-id")
    assert data == b"\xff\xd8jpeg-body"
    assert mime == "image/jpeg"


async def test_getfile_bad_request_is_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "wrong file_id"})

    with pytest.raises(TelegramFileNotFound):
        await _adapter(handler).fetch("bad")


async def test_getfile_too_big_is_too_large() -> None:
    """Пункт G: 400 "file is too big" - не «файла нет», а file_too_large (терминально)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"ok": False, "description": "Bad Request: file is too big"}
        )

    with pytest.raises(TelegramFileTooLarge):
        await _adapter(handler).fetch("huge")


async def test_download_404_is_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "a/b.png"}})
        return httpx.Response(404)

    with pytest.raises(TelegramFileNotFound):
        await _adapter(handler).fetch("gone")


async def test_server_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(TelegramFileTransient):
        await _adapter(handler).fetch("x")


async def test_too_many_requests_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"ok": False, "description": "Too Many Requests"})

    with pytest.raises(TelegramFileTransient):
        await _adapter(handler).fetch("x")


async def test_transport_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(TelegramFileTransient):
        await _adapter(handler).fetch("x")
