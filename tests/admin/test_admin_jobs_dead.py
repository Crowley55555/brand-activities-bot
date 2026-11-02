"""Фаза 10b-2 (I-1): /admin/jobs/dead — DLQ-UI с кнопками «повторить» и «похоронить».

Правило 10: без cookie — 302 на /admin/login; CSRF double-submit на POST.
Правило 2: сырой payload задачи в HTML не выводится.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.admin.conftest import CSRF_COOKIE, CreatedAdmin, sign_in
from tests.factories.booking import audit_rows


@pytest.fixture(autouse=True)
def _dlq_clean(sync_engine: Engine, clean_db: None) -> Any:
    with sync_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE audit_log RESTART IDENTITY CASCADE"))
    yield


def _insert_dead(
    engine: Engine,
    *,
    kind: str = "notify_reward",
    payload: dict[str, Any] | None = None,
    last_error: str = "boom",
    attempts: int = 6,
) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO jobs (kind, payload, status, attempts, run_after, "
                "  last_error, created_at, updated_at) "
                "VALUES (:kind, CAST(:payload AS jsonb), 'dead', :attempts, now(), "
                "  :err, now(), now()) "
                "RETURNING id"
            ),
            {
                "kind": kind,
                "payload": json.dumps(payload or {}),
                "attempts": attempts,
                "err": last_error,
            },
        ).one()
    return int(row.id)


def _insert_queued(engine: Engine, *, kind: str = "notify_reward") -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO jobs (kind, payload, status, attempts, run_after, "
                "  created_at, updated_at) "
                "VALUES (:kind, CAST('{}' AS jsonb), 'queued', 0, now(), now(), now()) "
                "RETURNING id"
            ),
            {"kind": kind},
        ).one()
    return int(row.id)


def _job_status(engine: Engine, job_id: int) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}
            ).scalar_one()
        )


async def _login(client: AsyncClient, factory: Callable[..., CreatedAdmin]) -> str:
    admin = factory(login="dlq-admin@test")
    await sign_in(client, admin)
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf is not None
    return csrf


# ---------------------------------- 9. GET без логина → 302 /admin/login


async def test_dead_page_requires_login(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/admin/jobs/dead")
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/admin/login")


# ---------------------------------------------------------------- 10. page lists dead only


async def test_dead_page_lists_dead_only(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    """В списке — только dead-задачи; queued рядом не видно."""
    jid_dead = _insert_dead(sync_engine, kind="notify_reward")
    jid_queued = _insert_queued(sync_engine, kind="notify_reward")
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.get("/admin/jobs/dead")
    assert resp.status_code == 200, resp.text
    assert f"data-job-id=\"{jid_dead}\"" in resp.text
    assert f"data-job-id=\"{jid_queued}\"" not in resp.text


# ---------------------------------------------------------------- 11. фильтр по kind


async def test_dead_page_filter_kind(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    jid_notify = _insert_dead(sync_engine, kind="notify_reward")
    jid_tg = _insert_dead(sync_engine, kind="telegram_send")
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.get("/admin/jobs/dead?kind=telegram_send")
    assert resp.status_code == 200
    assert f"data-job-id=\"{jid_tg}\"" in resp.text
    assert f"data-job-id=\"{jid_notify}\"" not in resp.text


# ---------------------------------------------------------------- 11a. payload сырой не выводится


async def test_dead_page_does_not_render_raw_payload(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    """Правило 2: payload задачи (может содержать chat_id/reward_id) не рисуется сырым JSON."""
    _insert_dead(
        sync_engine,
        kind="telegram_send",
        payload={"chat_id": 1234567890, "text": "secret-payload-marker"},
    )
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.get("/admin/jobs/dead")
    assert resp.status_code == 200
    assert "secret-payload-marker" not in resp.text
    assert "1234567890" not in resp.text


# ---------------------------------------------------------------- 12. revive без CSRF → 403


async def test_revive_post_without_csrf_403(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    jid = _insert_dead(sync_engine, kind="notify_reward")
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.post(f"/admin/jobs/dead/{jid}/revive", data={})
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"error": "csrf_invalid"}
    assert _job_status(sync_engine, jid) == "dead"


# ---------------------------------- 13. revive с CSRF → 302, статус queued


async def test_revive_post_success_302(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    jid = _insert_dead(sync_engine, kind="telegram_send")
    csrf = await _login(admin_client, admin_user_factory)

    resp = await admin_client.post(
        f"/admin/jobs/dead/{jid}/revive",
        data={"_csrf": csrf, "kind": "telegram_send"},
    )
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/admin/jobs/dead")
    # Фильтр kind сохраняется в URL-переадресации.
    assert "kind=telegram_send" in resp.headers["location"]
    assert _job_status(sync_engine, jid) == "queued"
    # Запись в журнале аудита.
    logged = audit_rows(sync_engine, "jobs_dead_revive")
    assert len(logged) == 1
    assert logged[0].object_type == "jobs"
    assert logged[0].object_id == jid


# ---------------------------------- 14. bury без reason → ошибка, статус dead


async def test_bury_post_requires_reason(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    jid = _insert_dead(sync_engine, kind="notify_reward")
    csrf = await _login(admin_client, admin_user_factory)

    resp = await admin_client.post(
        f"/admin/jobs/dead/{jid}/bury",
        data={"_csrf": csrf, "reason": ""},
    )
    # Либо 422, либо 302 с ошибкой на странице — важно, что задача не похоронена.
    assert resp.status_code in (200, 302, 422)
    assert _job_status(sync_engine, jid) == "dead"
    assert audit_rows(sync_engine, "jobs_dead_bury") == []


# ---------------------------------------------------------------- 15. bury success


async def test_bury_post_success_302(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    jid = _insert_dead(sync_engine, kind="notify_reward")
    csrf = await _login(admin_client, admin_user_factory)

    resp = await admin_client.post(
        f"/admin/jobs/dead/{jid}/bury",
        data={"_csrf": csrf, "reason": "manually_delivered"},
    )
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/admin/jobs/dead")
    assert _job_status(sync_engine, jid) == "done"
    logged = audit_rows(sync_engine, "jobs_dead_bury")
    assert len(logged) == 1
    assert logged[0].object_type == "jobs"
    assert logged[0].object_id == jid


# ---------------------------------------------------------------- 15a. bury без CSRF → 403


async def test_bury_post_without_csrf_403(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    jid = _insert_dead(sync_engine, kind="notify_reward")
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.post(
        f"/admin/jobs/dead/{jid}/bury",
        data={"reason": "manually_delivered"},  # без _csrf
    )
    assert resp.status_code == 403, resp.text
    assert _job_status(sync_engine, jid) == "dead"
