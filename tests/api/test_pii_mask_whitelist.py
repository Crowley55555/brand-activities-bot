"""Фаза 10a fix-1 (I-1): path-exempt PII-mask должен быть точным, а не префиксным.

Из трёх эндпоинтов `/internal/v1/channels/tg/fsm/*` PII в теле ответа возвращает
только `/get` (`FsmStateOut.data`). Остальные `/set` и `/clear` отдают `{"status":"ok"}` —
маскировать их безопасно. Любой будущий путь под тем же префиксом (например,
hypothetical `/debug`) не должен автоматически получать exemption — это нарушит
правило 2 без ревью.

Тесты поднимают мини-FastAPI с подключённым `PiiMaskResponseMiddleware` и
имитируют три пути. HMAC/БД здесь не нужны — проверяется именно middleware.
"""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.middleware.pii_mask import PiiMaskResponseMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(PiiMaskResponseMiddleware)

    @app.post("/internal/v1/channels/tg/fsm/get")
    async def _fsm_get() -> dict[str, object]:
        return {"state": "reg:wait_phone", "data": {"first_name": "Иван"}}

    @app.post("/internal/v1/channels/tg/fsm/set")
    async def _fsm_set() -> dict[str, object]:
        return {"status": "ok"}

    @app.post("/internal/v1/channels/tg/fsm/debug")
    async def _fsm_debug() -> dict[str, object]:
        return {"first_name": "Иван", "phone": "+79001234567"}

    return app


async def test_fsm_get_not_masked() -> None:
    """`/get` остаётся в allow-list: tg_worker должен получить реальное имя гостя."""
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/internal/v1/channels/tg/fsm/get")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "reg:wait_phone"
    assert body["data"] == {"first_name": "Иван"}


async def test_fsm_set_response_still_passthrough() -> None:
    """`/set` возвращает `{"status":"ok"}` — middleware не ломает контракт при маскировании."""
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/internal/v1/channels/tg/fsm/set")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_fsm_unknown_path_is_masked() -> None:
    """Hypothetical `/debug` под префиксом НЕ получает exemption — PII маскируется."""
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/internal/v1/channels/tg/fsm/debug")
    assert resp.status_code == 200
    body = resp.json()
    assert body["first_name"] == "***"
    assert body["phone"] == "***"
