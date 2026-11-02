"""Фаза 7b - /admin/rewards: список промокодов и ваучеров с фильтрами (GET-only)."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.admin.conftest import CreatedAdmin, sign_in
from tests.factories import create_attempt, create_guest

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
BRAND_A = "brand_a_skin_survey"
BRAND_B = "brand_b_photo_subscription"


@pytest.fixture(autouse=True)
def _rewards_clean(clean_db: None) -> None:
    return None


def _reward(
    engine: Engine,
    reward_type: str,
    code: str | None,
    *,
    slug: str = BRAND_A,
    redeemed: bool = False,
    revoked: bool = False,
) -> int:
    guest = create_guest(engine, f"ext-{code}-{reward_type}")
    attempt_id = create_attempt(engine, guest, slug)
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO rewards (attempt_id, reward_type, code, title, issued_at, "
                    "redeemed_at, revoked_at, wheel_prize_id, created_at, updated_at) "
                    "VALUES (:aid, :rt, :code, :title, :ts, "
                    "CASE WHEN :red THEN CAST(:ts AS timestamptz) END, "
                    "CASE WHEN :rev THEN CAST(:ts AS timestamptz) END, "
                    "CASE WHEN :rt = 'wheel_prize' THEN (SELECT min(id) FROM wheel_prizes) END, "
                    ":ts, :ts) "
                    "RETURNING id"
                ),
                {
                    "aid": attempt_id,
                    "rt": reward_type,
                    "code": code if reward_type in ("promocode", "voucher") else None,
                    "title": f"T-{code}",
                    "ts": NOW,
                    "red": redeemed,
                    "rev": revoked,
                },
            ).scalar_one()
        )


async def _login(client: AsyncClient, factory: Callable[..., CreatedAdmin]) -> None:
    await sign_in(client, factory(login="rewards-admin@test"))


def _count_rows(html: str) -> int:
    return len(re.findall(r'<tr class="reward"', html))


async def test_rewards_without_session_redirects_to_login(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/admin/rewards")
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/admin/login")


async def test_rewards_default_shows_only_promocodes_issued(
    admin_client: AsyncClient, admin_user_factory: Callable[..., CreatedAdmin], sync_engine: Engine
) -> None:
    _reward(sync_engine, "promocode", "PROMO-ISSUED")
    _reward(sync_engine, "voucher", "VOUCH-ISSUED")
    _reward(sync_engine, "promocode", "PROMO-REDEEMED", redeemed=True)
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.get("/admin/rewards")

    assert resp.status_code == 200, resp.text
    assert "PROMO-ISSUED" in resp.text
    assert "VOUCH-ISSUED" not in resp.text
    assert "PROMO-REDEEMED" not in resp.text


async def test_rewards_filter_type_voucher_shows_only_vouchers(
    admin_client: AsyncClient, admin_user_factory: Callable[..., CreatedAdmin], sync_engine: Engine
) -> None:
    _reward(sync_engine, "promocode", "PROMO-A")
    _reward(sync_engine, "voucher", "VOUCH-A")
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.get("/admin/rewards?type=voucher")

    assert "VOUCH-A" in resp.text
    assert "PROMO-A" not in resp.text


async def test_rewards_filter_type_all_shows_promocodes_and_vouchers_only(
    admin_client: AsyncClient, admin_user_factory: Callable[..., CreatedAdmin], sync_engine: Engine
) -> None:
    _reward(sync_engine, "promocode", "PROMO-A")
    _reward(sync_engine, "voucher", "VOUCH-A")
    _reward(sync_engine, "wheel_prize", "WHEEL-A")
    _reward(sync_engine, "leaflet", "LEAF-A")
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.get("/admin/rewards?type=all")

    assert "PROMO-A" in resp.text
    assert "VOUCH-A" in resp.text
    assert "WHEEL-A" not in resp.text
    assert "LEAF-A" not in resp.text


async def test_rewards_filter_status_redeemed_shows_only_redeemed(
    admin_client: AsyncClient, admin_user_factory: Callable[..., CreatedAdmin], sync_engine: Engine
) -> None:
    _reward(sync_engine, "promocode", "P-ISSUED")
    _reward(sync_engine, "promocode", "P-REDEEMED", redeemed=True)
    _reward(sync_engine, "promocode", "P-REVOKED", revoked=True)
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.get("/admin/rewards?status=redeemed")

    assert "P-REDEEMED" in resp.text
    assert "P-ISSUED" not in resp.text
    assert "P-REVOKED" not in resp.text


async def test_rewards_filter_activity_slug(
    admin_client: AsyncClient, admin_user_factory: Callable[..., CreatedAdmin], sync_engine: Engine
) -> None:
    _reward(sync_engine, "promocode", "P-BIO", slug=BRAND_A)
    _reward(sync_engine, "promocode", "P-REP", slug=BRAND_B)
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.get(f"/admin/rewards?activity_slug={BRAND_B}")

    assert "P-REP" in resp.text
    assert "P-BIO" not in resp.text


async def test_rewards_pagination(
    admin_client: AsyncClient, admin_user_factory: Callable[..., CreatedAdmin], sync_engine: Engine
) -> None:
    for i in range(60):
        _reward(sync_engine, "promocode", f"PG-{i:03d}")
    await _login(admin_client, admin_user_factory)

    page1 = await admin_client.get("/admin/rewards")
    page2 = await admin_client.get("/admin/rewards?page=2")

    assert _count_rows(page1.text) == 50
    assert _count_rows(page2.text) == 10


async def test_rewards_shows_user_id_not_phone(
    admin_client: AsyncClient, admin_user_factory: Callable[..., CreatedAdmin], sync_engine: Engine
) -> None:
    rid = _reward(sync_engine, "promocode", "P-PII")
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE users SET phone = '+79161234567', first_name = 'Артём', "
                "email = 'a@example.com'"
            )
        )
        uid = conn.execute(
            text(
                "SELECT a.user_id FROM rewards r JOIN attempts a ON a.id = r.attempt_id "
                "WHERE r.id = :id"
            ),
            {"id": rid},
        ).scalar_one()
    await _login(admin_client, admin_user_factory)

    resp = await admin_client.get("/admin/rewards")

    assert f'data-user-id="{uid}"' in resp.text
    assert "+7" not in resp.text
    assert "Артём" not in resp.text
    assert "a@example.com" not in resp.text
