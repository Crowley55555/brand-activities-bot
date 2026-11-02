"""/internal/v1/channels/tg/fsm (C-4, фаза 10a): персистентный FSM-storage.

Правило 10 (auth): все методы под HMAC — без заголовка 401. Правило 4 (внешние ресурсы
через порт): tg_worker ходит сюда, прямого доступа к БД у него нет.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from httpx import AsyncClient

from tests.conftest import TEST_HMAC


def _sign(body: bytes) -> str:
    return hmac.new(TEST_HMAC.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post(payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode("utf-8")
    return body, {
        "Content-Type": "application/json",
        "X-Internal-Auth": _sign(body),
    }


async def test_fsm_set_requires_hmac(api_client: AsyncClient) -> None:
    """PUT/set без HMAC → 401 missing_auth."""
    body = json.dumps({"key": "b:c:u:none:none:default", "state": None, "data": {}})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/set",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


async def test_fsm_set_get_clear_round_trip(api_client: AsyncClient, clean_db: None) -> None:
    """set → get → clear → get: данные ходят туда-обратно."""
    key = "bot:1:2:none:none:default"

    body, headers = _post({"key": key, "state": "reg:wait_phone", "data": {"first_name": "Иван"}})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/set", content=body, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    body, headers = _post({"key": key})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/get", content=body, headers=headers
    )
    assert resp.status_code == 200
    body_out = resp.json()
    assert body_out["state"] == "reg:wait_phone"
    assert body_out["data"] == {"first_name": "Иван"}

    body, headers = _post({"key": key})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/clear", content=body, headers=headers
    )
    assert resp.status_code == 200

    body, headers = _post({"key": key})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/get", content=body, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json() == {"state": None, "data": {}}


async def test_fsm_get_missing_key_returns_empty(api_client: AsyncClient, clean_db: None) -> None:
    """get по неизвестному ключу → 200 {state: null, data: {}}."""
    body, headers = _post({"key": "bot:999:999:none:none:default"})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/get", content=body, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json() == {"state": None, "data": {}}


async def test_fsm_set_replaces_state_and_data(
    api_client: AsyncClient, clean_db: None
) -> None:
    """Повторный set по тому же ключу заменяет state и data целиком."""
    key = "bot:1:3:none:none:default"
    # Первое значение
    body, headers = _post({"key": key, "state": "step1", "data": {"a": 1}})
    await api_client.post("/internal/v1/channels/tg/fsm/set", content=body, headers=headers)
    # Второе значение полностью заменяет data (а не merge)
    body, headers = _post({"key": key, "state": "step2", "data": {"b": 2}})
    await api_client.post("/internal/v1/channels/tg/fsm/set", content=body, headers=headers)

    body, headers = _post({"key": key})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/get", content=body, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json() == {"state": "step2", "data": {"b": 2}}


async def test_fsm_clear_is_idempotent(api_client: AsyncClient, clean_db: None) -> None:
    """clear по отсутствующему ключу → 200 (повтор безопасен)."""
    body, headers = _post({"key": "bot:1:4:none:none:default"})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/clear", content=body, headers=headers
    )
    assert resp.status_code == 200


# ===== N-2 (фаза 10b-3): /set_state_only и /set_data_only =====


async def test_set_state_only_leaves_data_intact(
    api_client: AsyncClient, clean_db: None
) -> None:
    """set_state_only(k, s=X) поверх строки с data={a:1} → state=X, data={a:1}."""
    key = "bot:1:5:none:none:default"
    # Положим data через полный /set.
    body, headers = _post({"key": key, "state": "step1", "data": {"a": 1}})
    await api_client.post("/internal/v1/channels/tg/fsm/set", content=body, headers=headers)

    # Теперь set_state_only — data не трогает.
    body, headers = _post({"key": key, "state": "step2"})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/set_state_only", content=body, headers=headers
    )
    assert resp.status_code == 200

    body, headers = _post({"key": key})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/get", content=body, headers=headers
    )
    assert resp.json() == {"state": "step2", "data": {"a": 1}}


async def test_set_data_only_leaves_state_intact(
    api_client: AsyncClient, clean_db: None
) -> None:
    """set_data_only(k, d={a:2}) поверх строки со state=X → state=X, data={a:2}."""
    key = "bot:1:6:none:none:default"
    body, headers = _post({"key": key, "state": "step1", "data": {"a": 1}})
    await api_client.post("/internal/v1/channels/tg/fsm/set", content=body, headers=headers)

    body, headers = _post({"key": key, "data": {"a": 2, "b": 3}})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/set_data_only", content=body, headers=headers
    )
    assert resp.status_code == 200

    body, headers = _post({"key": key})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/get", content=body, headers=headers
    )
    assert resp.json() == {"state": "step1", "data": {"a": 2, "b": 3}}


async def test_set_state_only_on_new_key_initializes_empty_data(
    api_client: AsyncClient, clean_db: None
) -> None:
    """set_state_only на новом ключе создаёт строку с data={}."""
    key = "bot:1:7:none:none:default"
    body, headers = _post({"key": key, "state": "fresh"})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/set_state_only", content=body, headers=headers
    )
    assert resp.status_code == 200

    body, headers = _post({"key": key})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/get", content=body, headers=headers
    )
    assert resp.json() == {"state": "fresh", "data": {}}


async def test_set_state_only_requires_hmac(api_client: AsyncClient) -> None:
    """set_state_only без HMAC → 401."""
    body = json.dumps({"key": "bot:1:9:none:none:default", "state": "x"})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/set_state_only",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


async def test_set_data_only_requires_hmac(api_client: AsyncClient) -> None:
    """set_data_only без HMAC → 401."""
    body = json.dumps({"key": "bot:1:9:none:none:default", "data": {}})
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/set_data_only",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


async def test_set_with_user_id_persists_user_id(
    api_client: AsyncClient, clean_db: None, sync_engine: object
) -> None:
    """/set с user_id сохраняет колонку (миграция 0021)."""
    key = "bot:1:10:none:none:default"
    body, headers = _post(
        {"key": key, "state": None, "data": {}, "user_id": 777}
    )
    resp = await api_client.post(
        "/internal/v1/channels/tg/fsm/set", content=body, headers=headers
    )
    assert resp.status_code == 200
    # Проверяем через sync-engine, что user_id записан.
    from sqlalchemy import text as sa_text

    with sync_engine.begin() as conn:  # type: ignore[attr-defined]
        uid = conn.execute(
            sa_text("SELECT user_id FROM tg_fsm_state WHERE key = :k"),
            {"k": key},
        ).scalar_one()
    assert int(uid) == 777
