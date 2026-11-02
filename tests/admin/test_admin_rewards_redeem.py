"""Фаза 10b-1 (C-1) - /admin/rewards/redeem: OTP-погашение именных наград.

Правило 10: без cookie - 302 на /admin/login; CSRF double-submit на POST.
Правило 9: карточка показывает маскированные имя/фамилию и `telegram_username`,
но не телефон/email.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.admin.conftest import CSRF_COOKIE, CreatedAdmin, sign_in
from tests.factories import create_attempt, create_guest

# ManualClock в tests/conftest.py фиксирован на 2026-10-12 05:00 UTC. Выравниваемся:
# issued_at у ваучера не должен быть позже clock.now (CHECK rewards_redeemed_after_issued),
# а истекший OTP — < clock.now в expires_at.
NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _redeem_clean(clean_db: None) -> None:
    return None


def _voucher(
    engine: Engine,
    guest_eid: str,
    *,
    first_name: str = "Иван",
    last_name: str = "Иванов",
    telegram_username: str | None = "ivan",
    otp: str | None = None,
    code: str = "VOUCHER-REDEEM01",
    expires_at: datetime | None = None,
) -> int:
    """Гость с ПДн + telegram_username + ваучер с OTP (если задан)."""
    guest = create_guest(engine, external_id=guest_eid, now=NOW)
    attempt_id = create_attempt(engine, guest, "brand_b_photo_subscription")
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE users SET first_name = :fn, last_name = :ln, "
                "registered_at = COALESCE(registered_at, :ts) WHERE id = :uid"
            ),
            {"fn": first_name, "ln": last_name, "ts": NOW, "uid": guest.user_id},
        )
        if telegram_username is not None:
            conn.execute(
                text(
                    "UPDATE channel_accounts SET external_username = :nick "
                    "WHERE user_id = :uid AND channel_type = 'telegram'"
                ),
                {"nick": telegram_username, "uid": guest.user_id},
            )
        row = conn.execute(
            text(
                "INSERT INTO rewards "
                "(attempt_id, reward_type, code, title, issued_at, is_bearer, "
                " redeem_otp, redeem_otp_expires_at, created_at, updated_at) "
                "VALUES (:aid, 'voucher', :code, 'Напиток дня', :ts, false, "
                " :otp, :exp, :ts, :ts) "
                "RETURNING id"
            ),
            {
                "aid": attempt_id,
                "code": code,
                "ts": NOW,
                "otp": otp,
                "exp": expires_at,
            },
        ).one()
    return int(row.id)


async def _login(client: AsyncClient, factory: Callable[..., CreatedAdmin]) -> str:
    admin = factory(login="redeem-admin@test")
    await sign_in(client, admin)
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf is not None
    return csrf


# ----- 17. GET без логина -> 302 /admin/login --------------------------------


async def test_redeem_form_requires_login(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/admin/rewards/redeem")
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/admin/login")


# ----- 18. POST с валидным OTP -> карточка -----------------------------------


async def test_redeem_lookup_shows_card(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    _voucher(
        sync_engine,
        "9001",
        first_name="Иван",
        last_name="Петров",
        telegram_username="ivan_p",
        otp="123456",
        expires_at=NOW + timedelta(minutes=5),
    )
    csrf = await _login(admin_client, admin_user_factory)
    resp = await admin_client.post(
        "/admin/rewards/redeem",
        data={"_csrf": csrf, "otp": "123456"},
    )
    assert resp.status_code == 200, resp.text
    # Маскированные ПДн
    assert "И**н" in resp.text
    assert "П****в" in resp.text
    assert "@ivan_p" in resp.text
    # Полное имя не отрисовано (правило 9)
    assert "Иван Петров" not in resp.text
    # Кнопка «Погасил»
    assert "Погасил" in resp.text


# ----- 19. неверный OTP -> та же форма с ошибкой (200, не 404) ---------------


async def test_redeem_lookup_wrong_otp(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    _voucher(
        sync_engine,
        "9002",
        otp="222222",
        expires_at=NOW + timedelta(minutes=5),
    )
    csrf = await _login(admin_client, admin_user_factory)
    resp = await admin_client.post(
        "/admin/rewards/redeem",
        data={"_csrf": csrf, "otp": "999999"},
    )
    assert resp.status_code == 200, resp.text
    assert "Код не найден или истёк" in resp.text
    # Карточка не появилась
    assert "Карточка владельца" not in resp.text


# ----- 20. успешное погашение -> 302 на /admin/rewards/redeem ----------------


async def test_redeem_action_success(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    reward_id = _voucher(
        sync_engine,
        "9003",
        otp="333333",
        expires_at=NOW + timedelta(minutes=5),
    )
    csrf = await _login(admin_client, admin_user_factory)
    resp = await admin_client.post(
        f"/admin/rewards/redeem/{reward_id}",
        data={"_csrf": csrf, "otp": "333333"},
    )
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"] == "/admin/rewards/redeem"

    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT redeemed_at, redeemed_by_admin_id, redeem_otp "
                "FROM rewards WHERE id = :id"
            ),
            {"id": reward_id},
        ).one()
    assert row.redeemed_at is not None
    assert row.redeemed_by_admin_id is not None
    assert row.redeem_otp is None


# ----- 21. POST без CSRF -> 403 ----------------------------------------------


async def test_redeem_action_csrf_required(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    reward_id = _voucher(
        sync_engine,
        "9004",
        otp="444444",
        expires_at=NOW + timedelta(minutes=5),
    )
    await _login(admin_client, admin_user_factory)
    resp = await admin_client.post(
        f"/admin/rewards/redeem/{reward_id}",
        data={"otp": "444444"},  # нет _csrf
    )
    assert resp.status_code == 403, resp.text
    body = resp.json() if resp.headers.get("content-type", "").startswith(
        "application/json"
    ) else {}
    assert body.get("error") == "csrf_invalid"


# ----- Доп: истёкший OTP -> та же форма, карточки нет ------------------------


async def test_redeem_lookup_expired(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    _voucher(
        sync_engine,
        "9005",
        otp="555555",
        expires_at=NOW - timedelta(seconds=1),
    )
    csrf = await _login(admin_client, admin_user_factory)
    resp = await admin_client.post(
        "/admin/rewards/redeem",
        data={"_csrf": csrf, "otp": "555555"},
    )
    assert resp.status_code == 200
    assert "Код не найден или истёк" in resp.text
    assert "Карточка владельца" not in resp.text


# ----- fix-1 I-2: {reward_id} в URL не совпадает с владельцем OTP ------------


async def test_redeem_action_id_mismatch_fails(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
) -> None:
    """POST /admin/rewards/redeem/{r1.id} с OTP от r2 → 200 с ошибкой otp_mismatch;
    r2 остаётся непогашенной. Защищает от подделки `{reward_id}` в URL — ядро не
    должно гасить чужую награду под маской карточки открытого гостя."""
    r1_id = _voucher(
        sync_engine,
        "9006",
        first_name="Иван",
        last_name="Иванов",
        telegram_username="ivan_1",
        otp="606060",
        code="VOUCHER-M1",
        expires_at=NOW + timedelta(minutes=5),
    )
    r2_id = _voucher(
        sync_engine,
        "9007",
        first_name="Пётр",
        last_name="Петров",
        telegram_username="petr_2",
        otp="707070",
        code="VOUCHER-M2",
        expires_at=NOW + timedelta(minutes=5),
    )
    csrf = await _login(admin_client, admin_user_factory)

    # Карточка открыта на r1, OTP набран от r2 — ядро должно отказать.
    resp = await admin_client.post(
        f"/admin/rewards/redeem/{r1_id}",
        data={"_csrf": csrf, "otp": "707070"},
    )
    assert resp.status_code == 200, resp.text
    assert "Код не совпадает с открытой карточкой" in resp.text

    # Ни r1, ни r2 не должны быть погашены.
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, redeemed_at, redeem_otp FROM rewards "
                "WHERE id IN (:r1, :r2) ORDER BY id"
            ),
            {"r1": r1_id, "r2": r2_id},
        ).all()
    by_id = {int(row.id): row for row in rows}
    assert by_id[r1_id].redeemed_at is None
    assert by_id[r2_id].redeemed_at is None
    # OTP у r2 остался на месте (его не трогали)
    assert by_id[r2_id].redeem_otp == "707070"
