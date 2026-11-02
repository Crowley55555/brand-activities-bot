"""core.channels.mark_unlinked (п.9 ревью): блок бота проставляет unlinked_at один раз."""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Engine

from app.core.channels import mark_unlinked
from tests.factories import create_guest
from tests.factories.workers import NOW, channel_unlinked


async def test_mark_unlinked_hides_channel_once(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    guest = create_guest(sync_engine, external_id="9201", now=NOW)

    async with uow_factory() as uow:
        changed = await mark_unlinked(uow.repositories.channels, "telegram", "9201", NOW)
    assert changed is True
    assert channel_unlinked(sync_engine, guest) is True

    async with uow_factory() as uow:
        assert await uow.repositories.channels.find_active("telegram", "9201") is None

    async with uow_factory() as uow:
        again = await mark_unlinked(uow.repositories.channels, "telegram", "9201", NOW)
    assert again is False
