"""start_attempt с override_expires_at (решение п.4): для booking срок жизни - слот + 10 минут.

Без override работает прежний расчёт по activities.limits.stale_ttl_minutes (Бренд C: 120 мин).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.engine import Engine

from app.core.attempts import start_attempt
from app.core.types.ids import ChannelAccountId, UserId
from tests.factories import create_guest
from tests.factories.booking import BRAND_C_SLUG, NOW, attempt_row, clean_booking

SLOT_ONE_PM = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)


async def _start(uow_factory: Any, guest: Any, moment: datetime, override: datetime | None) -> Any:
    async with uow_factory() as uow:
        r = uow.repositories
        return await start_attempt(
            r.consents,
            r.catalog,
            r.attempts,
            UserId(guest.user_id),
            BRAND_C_SLUG,
            ChannelAccountId(guest.channel_account_id),
            moment,
            override_expires_at=override,
        )


async def test_start_attempt_uses_override_expires_at(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    clean_booking(sync_engine)
    guest = create_guest(sync_engine, external_id="6001", now=NOW)
    override = SLOT_ONE_PM + timedelta(minutes=10)

    started = await _start(uow_factory, guest, NOW, override)

    assert attempt_row(sync_engine, int(started.attempt_id)).expires_at == override


async def test_start_attempt_without_override_keeps_stale_ttl(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    clean_booking(sync_engine)
    guest = create_guest(sync_engine, external_id="6002", now=NOW)

    started = await _start(uow_factory, guest, NOW, None)

    # Бренд C: stale_ttl_minutes = 120 (сид 0012)
    assert attempt_row(sync_engine, int(started.attempt_id)).expires_at == NOW + timedelta(
        minutes=120
    )
