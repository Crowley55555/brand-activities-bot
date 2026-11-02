"""Фаза 7c - /admin/metrics: регистрации, завершённые прохождения, выдачи по дням (GET-only)."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.admin.conftest import CreatedAdmin, sign_in
from tests.factories import DEFAULT_NOW, create_attempt, create_guest

# manual_clock = DEFAULT_NOW = 2026-10-12 08:00 МСК -> «сегодня» 2026-10-12
TODAY = "2026-10-12"
BRAND_A = "brand_a_skin_survey"
BRAND_B = "brand_b_photo_subscription"


@pytest.fixture(autouse=True)
def _metrics_clean(clean_db: None) -> None:
    return None


def _register(engine: Engine, ext: str, when: datetime = DEFAULT_NOW) -> None:
    guest = create_guest(engine, ext)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET registered_at = :ts WHERE id = :uid"),
            {"ts": when, "uid": guest.user_id},
        )


def _reward(engine: Engine, ext: str, reward_type: str, slug: str) -> None:
    guest = create_guest(engine, ext)
    attempt_id = create_attempt(engine, guest, slug)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rewards (attempt_id, reward_type, code, issued_at, "
                "created_at, updated_at) VALUES (:aid, :rt, :code, :ts, :ts, :ts)"
            ),
            {"aid": attempt_id, "rt": reward_type, "code": f"C-{ext}", "ts": DEFAULT_NOW},
        )


async def _login(client: AsyncClient, factory: Callable[..., CreatedAdmin]) -> None:
    await sign_in(client, factory(login="metrics-admin@test"))


def _day_cells(html: str, day: str) -> list[str]:
    row = re.search(rf'<tr class="metric-day" data-day="{day}">(.*?)</tr>', html, re.S)
    assert row, f"нет строки {day}"
    return re.findall(r"<td>(.*?)</td>", row.group(1))


async def test_metrics_without_session_redirects_to_login(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/admin/metrics")
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/admin/login")


async def test_metrics_default_shows_seven_days(
    admin_client: AsyncClient, admin_user_factory: Callable[..., CreatedAdmin]
) -> None:
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.get("/admin/metrics")

    assert resp.status_code == 200, resp.text
    assert len(re.findall(r'<tr class="metric-day"', resp.text)) == 7
    assert _day_cells(resp.text, TODAY) == [TODAY, "0", "0", "0", "0"]
    assert 'data-day="2026-10-06"' in resp.text


async def test_metrics_counts_registrations_promocodes_vouchers(
    admin_client: AsyncClient, admin_user_factory: Callable[..., CreatedAdmin], sync_engine: Engine
) -> None:
    _register(sync_engine, "r1")
    _register(sync_engine, "r2")
    _register(sync_engine, "old", datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    _reward(sync_engine, "p1", "promocode", BRAND_A)  # завершённая попытка + промокод
    _reward(sync_engine, "v1", "voucher", BRAND_B)  # завершённая попытка + ваучер
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.get("/admin/metrics")

    # 2 регистрации; 2 завершённые попытки (по одной на награду); 1 промокод; 1 ваучер
    assert _day_cells(resp.text, TODAY) == [TODAY, "2", "2", "1", "1"]


async def test_metrics_brand_filter(
    admin_client: AsyncClient, admin_user_factory: Callable[..., CreatedAdmin], sync_engine: Engine
) -> None:
    _register(sync_engine, "r1")
    _reward(sync_engine, "p1", "promocode", BRAND_A)
    _reward(sync_engine, "v1", "voucher", BRAND_B)
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.get("/admin/metrics?brand_slug=brand_a")

    assert resp.status_code == 200
    assert _day_cells(resp.text, TODAY) == [TODAY, "0", "1", "1", "0"]


async def test_metrics_days_out_of_range_rejected(
    admin_client: AsyncClient, admin_user_factory: Callable[..., CreatedAdmin]
) -> None:
    await _login(admin_client, admin_user_factory)

    assert (await admin_client.get("/admin/metrics?days=100")).status_code in (400, 422)
    assert (await admin_client.get("/admin/metrics?days=0")).status_code in (400, 422)
