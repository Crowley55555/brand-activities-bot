from __future__ import annotations

import io
import json
import logging

import pytest


@pytest.fixture
def log_capture() -> io.StringIO:
    """Перехватываем stdlib root-logger вывод через StreamHandler."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        root.handlers[:] = old_handlers
        root.setLevel(old_level)


def test_structlog_pii_processor_masks_whitelisted_keys() -> None:
    from app.common.pii_mask import structlog_pii_processor

    event = {
        "event": "user_registered",
        "first_name": "Иван",
        "last_name": "Петров",
        "phone": "+79001234567",
        "email": "ivan@example.com",
        "telegram_username": "anna",
        "username": "anna",
        "user_id": 1,
    }
    masked = structlog_pii_processor(None, "info", event)
    assert masked["first_name"] == "***"
    assert masked["last_name"] == "***"
    assert masked["phone"] == "***"
    assert masked["email"] == "***"
    assert masked["telegram_username"] == "***"
    assert masked["username"] == "***"
    assert masked["user_id"] == 1
    assert masked["event"] == "user_registered"


def test_structlog_pii_processor_recurses_into_nested_dicts() -> None:
    from app.common.pii_mask import structlog_pii_processor

    event = {
        "event": "update_received",
        "message": {
            "from": {
                "first_name": "Иван",
                "username": "anna",
                "id": 12345,
            },
            "text": "/start",
        },
    }
    masked = structlog_pii_processor(None, "info", event)
    assert masked["message"]["from"]["first_name"] == "***"
    assert masked["message"]["from"]["username"] == "***"
    assert masked["message"]["from"]["id"] == 12345
    assert masked["message"]["text"] == "/start"


def test_setup_logging_configures_structlog_with_pii_mask(
    log_capture: io.StringIO,
) -> None:
    import structlog

    from app.common.logging_setup import setup_logging

    setup_logging()
    log = structlog.get_logger("test_logger")
    log.info(
        "user_registered",
        first_name="Иван",
        last_name="Петров",
        phone="+79001234567",
        email="ivan@example.com",
        telegram_username="anna",
        user_id=1,
    )
    out = log_capture.getvalue()
    # В выводе не должно быть сырых ПДн.
    assert "Иван" not in out
    assert "Петров" not in out
    assert "+79001234567" not in out
    assert "ivan@example.com" not in out
    assert "anna" not in out
    # Нечувствительные поля остались видны.
    assert "user_registered" in out
    assert "user_id" in out
    # Маскирование применено.
    assert "***" in out


async def test_health_endpoint_response_has_no_pii_fields() -> None:
    """Прошли /health — не содержит ПДн."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    data = json.loads(resp.text)
    assert "first_name" not in resp.text
    assert "phone" not in resp.text
    assert "email" not in resp.text
    assert data == {"status": "ok"}


async def test_response_middleware_masks_pii_in_json_body() -> None:
    """ASGI-middleware маскирует ПДн в JSON-ответе."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api.middleware.pii_mask import PiiMaskResponseMiddleware

    app = FastAPI()
    app.add_middleware(PiiMaskResponseMiddleware)

    @app.get("/dump")
    async def _dump() -> dict[str, object]:
        return {
            "event": "ok",
            "first_name": "Иван",
            "user": {
                "last_name": "Петров",
                "phone": "+79001234567",
                "email": "IVAN@example.com",
                "telegram_username": "anna",
                "user_id": 7,
            },
        }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/dump")
    assert resp.status_code == 200
    data = resp.json()
    assert data["first_name"] == "***"
    assert data["user"]["last_name"] == "***"
    assert data["user"]["phone"] == "***"
    assert data["user"]["email"] == "***"
    assert data["user"]["telegram_username"] == "***"
    assert data["user"]["user_id"] == 7
    assert data["event"] == "ok"
