from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Final

from app.common.pii_mask import mask_pii

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

# Фаза 10a (C-4), fix-1 I-1: разрешаем «без маскирования» ТОЛЬКО /get FSM-storage.
# `FsmStateOut.data` (`first_name`, `phone` и т. п.) идёт tg_worker под HMAC во
# внутренний периметр — маскирование сломало бы контракт регистрации; правило 9
# не нарушено (внутренний HMAC-канал в docker-сети).
# `/set` и `/clear` возвращают `{"status":"ok"}` — PII в ответе нет, exemption
# не нужен; маскирование безопасно и бесплатно. Любой будущий путь под тем же
# префиксом не должен автоматически получать exemption без ревью.
_PII_EXEMPT_PATHS: Final[frozenset[str]] = frozenset(
    {"/internal/v1/channels/tg/fsm/get"}
)


def _is_exempt(path: str) -> bool:
    return path in _PII_EXEMPT_PATHS


class PiiMaskResponseMiddleware:
    """Маскирует PII-поля в теле JSON-ответов (правило 2).

    На response.start — проверяем Content-Type.
    На response.body — буферизуем, парсим JSON, маскируем, перепаковываем.
    Не трогаем бинарные и не-JSON ответы (passthrough).
    """

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if isinstance(path, str) and _is_exempt(path):
            await self._app(scope, receive, send)
            return

        is_json = False
        chunks: list[bytes] = []
        start_message: Message | None = None

        async def wrapped_send(message: Message) -> None:
            nonlocal is_json, start_message
            msg_type = message["type"]
            if msg_type == "http.response.start":
                start_message = message
                headers = message.get("headers") or []
                for raw_name, raw_value in headers:
                    if raw_name.lower() == b"content-type":
                        is_json = raw_value.split(b";", 1)[0].strip() == b"application/json"
                        break
                if not is_json:
                    await send(message)
                return

            if msg_type != "http.response.body":
                await send(message)
                return

            if not is_json:
                await send(message)
                return

            body = message.get("body") or b""
            more_body = bool(message.get("more_body"))
            chunks.append(body)
            if more_body:
                return

            raw = b"".join(chunks)
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError):
                # не-JSON — не трогаем
                assert start_message is not None
                await send(start_message)
                await send(message)
                return

            masked = mask_pii(parsed) if parsed is not None else None
            new_body = (
                json.dumps(masked, ensure_ascii=False).encode("utf-8")
                if masked is not None
                else b""
            )
            assert start_message is not None
            # пересобираем Content-Length в заголовках
            new_headers: list[tuple[bytes, bytes]] = []
            for raw_name, raw_value in start_message.get("headers", []) or []:
                if raw_name.lower() == b"content-length":
                    continue
                new_headers.append((raw_name, raw_value))
            new_headers.append((b"content-length", str(len(new_body)).encode("ascii")))
            start_message["headers"] = new_headers
            await send(start_message)
            await send(
                {
                    "type": "http.response.body",
                    "body": new_body,
                    "more_body": False,
                }
            )

        await self._app(scope, receive, wrapped_send)
