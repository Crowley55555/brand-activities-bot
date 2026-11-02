"""OTP-погашение именных наград (фаза 10b-1, C-1).

Канон 02 §7 / 03 §1 / 04 §1.2 / 09 §3. Идём через core без моков: правило 3 требует
бизнес-логику в core, а SqlRewardRepo — её прямой читатель. Привязка Telegram, имя
и фамилия пишутся напрямую через SQL (это фикстурные ПДн, собранные с действующим
consent): правило 1 для теста не нарушено, фабрика create_guest ставит consent.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.common.errors import ConflictError
from app.core.rewards.otp import (
    OTP_LENGTH,
    find_by_otp,
    redeem_with_otp,
    request_otp,
)
from app.core.types.ids import AdminId, RewardId, UserId
from tests.factories import create_attempt, create_guest

NOW = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
TTL = 5  # минут


def _create_voucher(
    engine: Engine,
    *,
    attempt_id: int,
    code: str = "VOUCHER-TEST01",
    title: str = "Напиток дня",
    issued_at: datetime = NOW,
) -> int:
    """Ваучер (reward_type='voucher'), is_bearer=false — именная награда."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO rewards "
                "(attempt_id, reward_type, code, title, issued_at, is_bearer, "
                " created_at, updated_at) "
                "VALUES (:aid, 'voucher', :code, :title, :ts, false, :ts, :ts) "
                "RETURNING id"
            ),
            {"aid": attempt_id, "code": code, "title": title, "ts": issued_at},
        ).one()
    return int(row.id)


def _create_promocode(
    engine: Engine,
    *,
    attempt_id: int,
    code: str = "PROMO-TEST01",
    issued_at: datetime = NOW,
) -> int:
    """Промокод (reward_type='promocode'), is_bearer=true — предъявительская."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO rewards "
                "(attempt_id, reward_type, code, issued_at, is_bearer, "
                " created_at, updated_at) "
                "VALUES (:aid, 'promocode', :code, :ts, true, :ts, :ts) "
                "RETURNING id"
            ),
            {"aid": attempt_id, "code": code, "ts": issued_at},
        ).one()
    return int(row.id)


def _set_user_pii(
    engine: Engine,
    user_id: int,
    *,
    first_name: str,
    last_name: str,
    telegram_username: str | None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE users SET first_name = :fn, last_name = :ln, "
                "registered_at = COALESCE(registered_at, now()) "
                "WHERE id = :uid"
            ),
            {"fn": first_name, "ln": last_name, "uid": user_id},
        )
        if telegram_username is not None:
            conn.execute(
                text(
                    "UPDATE channel_accounts SET external_username = :nick "
                    "WHERE user_id = :uid AND channel_type = 'telegram'"
                ),
                {"nick": telegram_username, "uid": user_id},
            )


def _anonymize(engine: Engine, user_id: int, now: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE users SET first_name = NULL, last_name = NULL, "
                "phone = NULL, email = NULL, anonymized_at = :ts "
                "WHERE id = :uid"
            ),
            {"ts": now, "uid": user_id},
        )


def _create_admin(engine: Engine, display_name: str = "Админ тест") -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO admin_users "
                "(login, password_hash, display_name, is_active, created_at, updated_at) "
                "VALUES (:lg, 'x', :dn, true, now(), now()) RETURNING id"
            ),
            {"lg": f"admin-otp-{display_name}", "dn": display_name},
        ).one()
    return int(row.id)


def _seeded_otp_source(value: str) -> Callable[[], str]:
    def _source() -> str:
        return value

    return _source


def _reward_row(engine: Engine, reward_id: int) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT redeem_otp, redeem_otp_expires_at, redeemed_at, "
                "redeemed_by_admin_id FROM rewards WHERE id = :id"
            ),
            {"id": reward_id},
        ).one()


# ----- 5. request_otp только для именных ---------------------------------------


async def test_request_otp_only_for_named_rewards(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """voucher получает OTP 6 цифр и expires_at = now + 5 мин; promocode -> reward_not_eligible."""
    guest = create_guest(sync_engine, external_id="7001", now=NOW)
    attempt = create_attempt(sync_engine, guest, "brand_b_photo_subscription")
    voucher_id = _create_voucher(sync_engine, attempt_id=attempt)
    promocode_id = _create_promocode(sync_engine, attempt_id=attempt)

    source = _seeded_otp_source("654321")
    async with uow_factory() as uow:
        issued = await request_otp(
            uow.repositories.rewards,
            RewardId(voucher_id),
            UserId(guest.user_id),
            NOW,
            TTL,
            source,
        )
    assert issued.otp == "654321"
    assert len(issued.otp) == OTP_LENGTH
    assert issued.expires_at == NOW + timedelta(minutes=TTL)
    row = _reward_row(sync_engine, voucher_id)
    assert row.redeem_otp == "654321"
    assert row.redeem_otp_expires_at == issued.expires_at

    async with uow_factory() as uow:
        with pytest.raises(ConflictError) as info:
            await request_otp(
                uow.repositories.rewards,
                RewardId(promocode_id),
                UserId(guest.user_id),
                NOW,
                TTL,
                source,
            )
    assert info.value.code == "reward_not_eligible"
    # у промокода OTP не записался
    row = _reward_row(sync_engine, promocode_id)
    assert row.redeem_otp is None
    assert row.redeem_otp_expires_at is None


# ----- 6. повторный request_otp перезаписывает OTP -----------------------------


async def test_request_otp_rewrites_previous(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Второй вызов перетирает OTP; старый код через find_by_otp уже не находится."""
    guest = create_guest(sync_engine, external_id="7002", now=NOW)
    attempt = create_attempt(sync_engine, guest, "brand_b_photo_subscription")
    voucher_id = _create_voucher(sync_engine, attempt_id=attempt)
    _set_user_pii(
        sync_engine,
        guest.user_id,
        first_name="Иван",
        last_name="Иванов",
        telegram_username="ivan",
    )

    source1 = _seeded_otp_source("111111")
    source2 = _seeded_otp_source("222222")
    async with uow_factory() as uow:
        await request_otp(
            uow.repositories.rewards,
            RewardId(voucher_id),
            UserId(guest.user_id),
            NOW,
            TTL,
            source1,
        )
    async with uow_factory() as uow:
        issued2 = await request_otp(
            uow.repositories.rewards,
            RewardId(voucher_id),
            UserId(guest.user_id),
            NOW + timedelta(minutes=1),
            TTL,
            source2,
        )
    assert issued2.otp == "222222"

    async with uow_factory() as uow:
        card_old = await find_by_otp(
            uow.repositories.rewards, "111111", NOW + timedelta(minutes=2)
        )
        card_new = await find_by_otp(
            uow.repositories.rewards, "222222", NOW + timedelta(minutes=2)
        )
    assert card_old is None
    assert card_new is not None
    assert card_new.reward_id == RewardId(voucher_id)


# ----- 7. find_by_otp отдаёт маскированные ПДн + telegram_username -------------


async def test_find_by_otp_returns_card_with_masked_pii(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Карточка под активным OTP: имя/фамилия маскированные, telegram_username как есть."""
    guest = create_guest(sync_engine, external_id="7003", now=NOW)
    attempt = create_attempt(sync_engine, guest, "brand_b_photo_subscription")
    voucher_id = _create_voucher(
        sync_engine, attempt_id=attempt, title="Напиток дня", code="VOUCHER-ABCDEF"
    )
    _set_user_pii(
        sync_engine,
        guest.user_id,
        first_name="Иван",
        last_name="Петров",
        telegram_username="ivan_p",
    )

    source = _seeded_otp_source("333444")
    async with uow_factory() as uow:
        await request_otp(
            uow.repositories.rewards,
            RewardId(voucher_id),
            UserId(guest.user_id),
            NOW,
            TTL,
            source,
        )
    async with uow_factory() as uow:
        card = await find_by_otp(uow.repositories.rewards, "333444", NOW)
    assert card is not None
    # «Иван» -> «И**н» (длина 4 -> первая, 2 звезды, последняя)
    assert card.first_name_masked == "И**н"
    # «Петров» -> «П****в» (длина 6 -> первая, 4 звезды, последняя)
    assert card.last_name_masked == "П****в"
    assert card.telegram_username == "ivan_p"
    # Телефон/email в DTO не предусмотрены вообще (структурный запрет)
    assert not hasattr(card, "phone")
    assert not hasattr(card, "email")
    assert card.reward_id == RewardId(voucher_id)
    assert card.title == "Напиток дня"


# ----- 8. find_by_otp истёкший -> None (freezegun для стабильности) ------------


async def test_find_by_otp_expired_returns_none(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """OTP с expires_at = now - 1s -> None (не выдаём карточку)."""
    guest = create_guest(sync_engine, external_id="7004", now=NOW)
    attempt = create_attempt(sync_engine, guest, "brand_b_photo_subscription")
    voucher_id = _create_voucher(sync_engine, attempt_id=attempt)
    _set_user_pii(
        sync_engine,
        guest.user_id,
        first_name="Анна",
        last_name="Сидорова",
        telegram_username="anna_s",
    )

    source = _seeded_otp_source("555666")
    with freeze_time(NOW):
        async with uow_factory() as uow:
            await request_otp(
                uow.repositories.rewards,
                RewardId(voucher_id),
                UserId(guest.user_id),
                NOW,
                TTL,
                source,
            )

    # Сдвигаем часы вперёд на TTL + 1 секунду: expires_at <= now
    future = NOW + timedelta(minutes=TTL, seconds=1)
    with freeze_time(future):
        async with uow_factory() as uow:
            card = await find_by_otp(uow.repositories.rewards, "555666", future)
    assert card is None


# ----- 9. find_by_otp уже погашен -> None --------------------------------------


async def test_find_by_otp_already_redeemed_returns_none(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    guest = create_guest(sync_engine, external_id="7005", now=NOW)
    attempt = create_attempt(sync_engine, guest, "brand_b_photo_subscription")
    voucher_id = _create_voucher(sync_engine, attempt_id=attempt)
    _set_user_pii(
        sync_engine,
        guest.user_id,
        first_name="Пётр",
        last_name="Петров",
        telegram_username="petr",
    )
    admin_id = _create_admin(sync_engine, "r9")

    async with uow_factory() as uow:
        await request_otp(
            uow.repositories.rewards,
            RewardId(voucher_id),
            UserId(guest.user_id),
            NOW,
            TTL,
            _seeded_otp_source("777888"),
        )
    async with uow_factory() as uow:
        await redeem_with_otp(uow.repositories.rewards, "777888", AdminId(admin_id), NOW)

    async with uow_factory() as uow:
        card = await find_by_otp(uow.repositories.rewards, "777888", NOW)
    assert card is None


# ----- 10. find_by_otp для анонимизированного гостя -> ConflictError ----------


async def test_find_by_otp_anonymized_user_rejects(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    guest = create_guest(sync_engine, external_id="7006", now=NOW)
    attempt = create_attempt(sync_engine, guest, "brand_b_photo_subscription")
    voucher_id = _create_voucher(sync_engine, attempt_id=attempt)
    _set_user_pii(
        sync_engine,
        guest.user_id,
        first_name="Иван",
        last_name="Иванов",
        telegram_username="iv",
    )
    async with uow_factory() as uow:
        await request_otp(
            uow.repositories.rewards,
            RewardId(voucher_id),
            UserId(guest.user_id),
            NOW,
            TTL,
            _seeded_otp_source("909090"),
        )
    _anonymize(sync_engine, guest.user_id, NOW)

    async with uow_factory() as uow:
        with pytest.raises(ConflictError) as info:
            await find_by_otp(uow.repositories.rewards, "909090", NOW)
    assert info.value.code == "user_anonymized"


# ----- 11. redeem_with_otp успешный путь --------------------------------------


async def test_redeem_with_otp_sets_redeemed_at_and_admin_id(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    guest = create_guest(sync_engine, external_id="7007", now=NOW)
    attempt = create_attempt(sync_engine, guest, "brand_b_photo_subscription")
    voucher_id = _create_voucher(sync_engine, attempt_id=attempt)
    admin_id = _create_admin(sync_engine, "r11")

    async with uow_factory() as uow:
        await request_otp(
            uow.repositories.rewards,
            RewardId(voucher_id),
            UserId(guest.user_id),
            NOW,
            TTL,
            _seeded_otp_source("121212"),
        )
    async with uow_factory() as uow:
        reward_id = await redeem_with_otp(
            uow.repositories.rewards, "121212", AdminId(admin_id), NOW
        )
    assert reward_id == RewardId(voucher_id)

    row = _reward_row(sync_engine, voucher_id)
    assert row.redeemed_at is not None
    assert row.redeemed_by_admin_id == admin_id


# ----- 12. redeem_with_otp второй раз -> already_redeemed ---------------------


async def test_redeem_with_otp_twice_second_fails(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    guest = create_guest(sync_engine, external_id="7008", now=NOW)
    attempt = create_attempt(sync_engine, guest, "brand_b_photo_subscription")
    voucher_id = _create_voucher(sync_engine, attempt_id=attempt)
    admin_id = _create_admin(sync_engine, "r12")

    async with uow_factory() as uow:
        await request_otp(
            uow.repositories.rewards,
            RewardId(voucher_id),
            UserId(guest.user_id),
            NOW,
            TTL,
            _seeded_otp_source("131313"),
        )
    async with uow_factory() as uow:
        await redeem_with_otp(uow.repositories.rewards, "131313", AdminId(admin_id), NOW)

    async with uow_factory() as uow:
        with pytest.raises(ConflictError) as info:
            await redeem_with_otp(
                uow.repositories.rewards, "131313", AdminId(admin_id), NOW
            )
    assert info.value.code == "otp_not_found"


# ----- 13. redeem сбрасывает redeem_otp и expires_at --------------------------


async def test_redeem_with_otp_clears_otp_fields(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """После погашения redeem_otp = NULL и redeem_otp_expires_at = NULL."""
    guest = create_guest(sync_engine, external_id="7009", now=NOW)
    attempt = create_attempt(sync_engine, guest, "brand_b_photo_subscription")
    voucher_id = _create_voucher(sync_engine, attempt_id=attempt)
    admin_id = _create_admin(sync_engine, "r13")

    async with uow_factory() as uow:
        await request_otp(
            uow.repositories.rewards,
            RewardId(voucher_id),
            UserId(guest.user_id),
            NOW,
            TTL,
            _seeded_otp_source("141414"),
        )
    async with uow_factory() as uow:
        await redeem_with_otp(uow.repositories.rewards, "141414", AdminId(admin_id), NOW)

    row = _reward_row(sync_engine, voucher_id)
    assert row.redeem_otp is None
    assert row.redeem_otp_expires_at is None


# ----- Доп. бонус: already_redeemed при повторном request_otp на погашенной ----


async def test_request_otp_on_redeemed_rejects(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """После погашения повтор `request_otp` отвечает already_redeemed, OTP не ставится."""
    guest = create_guest(sync_engine, external_id="7010", now=NOW)
    attempt = create_attempt(sync_engine, guest, "brand_b_photo_subscription")
    voucher_id = _create_voucher(sync_engine, attempt_id=attempt)
    admin_id = _create_admin(sync_engine, "r14")

    async with uow_factory() as uow:
        await request_otp(
            uow.repositories.rewards,
            RewardId(voucher_id),
            UserId(guest.user_id),
            NOW,
            TTL,
            _seeded_otp_source("151515"),
        )
    async with uow_factory() as uow:
        await redeem_with_otp(uow.repositories.rewards, "151515", AdminId(admin_id), NOW)

    async with uow_factory() as uow:
        with pytest.raises(ConflictError) as info:
            await request_otp(
                uow.repositories.rewards,
                RewardId(voucher_id),
                UserId(guest.user_id),
                NOW + timedelta(minutes=10),
                TTL,
                _seeded_otp_source("161616"),
            )
    assert info.value.code == "already_redeemed"


# ----- fix-1 I-1: коллизия partial unique rewards_active_otp -------------------


def _iter_otp_source(values: list[str]) -> Callable[[], str]:
    """Детерминированный источник OTP, возвращает значения по порядку."""
    it = iter(values)

    def _source() -> str:
        return next(it)

    return _source


async def test_request_otp_retries_on_collision(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Два живых OTP с одним значением — коллизия; ядро повторяет до уникального.

    Гость 1 уже получил OTP `"111111"`. Гость 2 просит OTP: источник выдаёт
    `["111111", "111111", "222222"]` — первые две попытки упираются в partial
    unique `rewards_active_otp`, третья проходит. Проверяем, что `request_otp`
    вернул `"222222"` и у ваучера гостя 2 записан именно он.
    """
    guest1 = create_guest(sync_engine, external_id="7101", now=NOW)
    attempt1 = create_attempt(sync_engine, guest1, "brand_b_photo_subscription")
    voucher1_id = _create_voucher(
        sync_engine, attempt_id=attempt1, code="VOUCHER-R1-01"
    )

    guest2 = create_guest(sync_engine, external_id="7102", now=NOW)
    attempt2 = create_attempt(sync_engine, guest2, "brand_b_photo_subscription")
    voucher2_id = _create_voucher(
        sync_engine, attempt_id=attempt2, code="VOUCHER-R1-02"
    )

    # Гость 1 получает OTP "111111"
    async with uow_factory() as uow:
        await request_otp(
            uow.repositories.rewards,
            RewardId(voucher1_id),
            UserId(guest1.user_id),
            NOW,
            TTL,
            _seeded_otp_source("111111"),
        )

    # Гость 2: source выдаёт "111111" (коллизия), "111111" (коллизия), "222222" (ok)
    async with uow_factory() as uow:
        issued = await request_otp(
            uow.repositories.rewards,
            RewardId(voucher2_id),
            UserId(guest2.user_id),
            NOW,
            TTL,
            _iter_otp_source(["111111", "111111", "222222"]),
        )

    assert issued.otp == "222222"
    row2 = _reward_row(sync_engine, voucher2_id)
    assert row2.redeem_otp == "222222"
    # OTP гостя 1 не затронут
    row1 = _reward_row(sync_engine, voucher1_id)
    assert row1.redeem_otp == "111111"


async def test_request_otp_collision_exhaustion_raises(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Если каждая попытка — коллизия, после MAX_OTP_ATTEMPTS → otp_collision.

    Гость 1 держит OTP `"111111"`. Гость 2 ловит source, который всегда отдаёт
    `"111111"` — ядро через MAX_OTP_ATTEMPTS попыток бросит
    `ConflictError('otp_collision')`. У гостя 2 OTP не записан (строка пустая).
    """
    guest1 = create_guest(sync_engine, external_id="7103", now=NOW)
    attempt1 = create_attempt(sync_engine, guest1, "brand_b_photo_subscription")
    voucher1_id = _create_voucher(
        sync_engine, attempt_id=attempt1, code="VOUCHER-R1-03"
    )

    guest2 = create_guest(sync_engine, external_id="7104", now=NOW)
    attempt2 = create_attempt(sync_engine, guest2, "brand_b_photo_subscription")
    voucher2_id = _create_voucher(
        sync_engine, attempt_id=attempt2, code="VOUCHER-R1-04"
    )

    async with uow_factory() as uow:
        await request_otp(
            uow.repositories.rewards,
            RewardId(voucher1_id),
            UserId(guest1.user_id),
            NOW,
            TTL,
            _seeded_otp_source("111111"),
        )

    async with uow_factory() as uow:
        with pytest.raises(ConflictError) as info:
            await request_otp(
                uow.repositories.rewards,
                RewardId(voucher2_id),
                UserId(guest2.user_id),
                NOW,
                TTL,
                _seeded_otp_source("111111"),
            )
    assert info.value.code == "otp_collision"

    # Гость 2 остался без OTP (попытки не писали строку)
    row2 = _reward_row(sync_engine, voucher2_id)
    assert row2.redeem_otp is None
    assert row2.redeem_otp_expires_at is None
