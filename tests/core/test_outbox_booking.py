"""Шаблоны outbox фазы 4: appointment_reminder, appointment_auto_cancelled, appointment_leaflet.

Воркер кладёт в telegram_send только id записи (без ПДн и без текста). Текст собирает claim_outbox
из БД: название активности и время слота по Москве (13:00 при slot 10:00 UTC), минуты и листовка -
из activities.limits. Тексты задаёт конфиг; здесь - фиксированные строки для проверки подстановки.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Engine

from app.core.activities.booking import mark_auto_cancelled, mark_reminded
from app.core.jobs.outbox import OutboxTexts, claim_outbox
from app.core.types.ids import AppointmentId
from tests.factories import create_guest
from tests.factories.booking import (
    BRAND_C_SLUG,
    NOW,
    SLOT_ONE_PM,
    appointment_row,
    arrive_as,
    book_as,
    brand_id,
    clean_booking,
    create_booking_admin,
    grant_brand,
    make_slot,
)

TEXTS = OutboxTexts(
    reward_notice="",
    ugc_rejected_retry="",
    ugc_rejected_closed="",
    ingest_failed_retry="",
    ingest_failed_closed="",
    appointment_reminder="Напоминание: через {minutes} мин «{activity}» в {time}.",
    appointment_auto_cancelled="Отменено: «{activity}» на {time}.",
    appointment_leaflet="Листовка «{title}»: {url}",
    public_base_url="https://test.example",
)


async def _claim(uow_factory: Any) -> list[Any]:
    async with uow_factory() as uow:
        r = uow.repositories
        return await claim_outbox(
            r.jobs,
            r.rewards,
            r.catalog,
            TEXTS,
            worker_id="t-outbox",
            limit=20,
            visibility_timeout_s=90,
            appointments=r.appointments,
        )


async def test_reminder_text_is_rendered_from_db(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    clean_booking(sync_engine)
    guest = create_guest(sync_engine, external_id="7001", now=NOW)
    booked = await book_as(
        uow_factory, guest, BRAND_C_SLUG, make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    )
    async with uow_factory() as uow:
        await mark_reminded(uow.repositories, AppointmentId(int(booked.appointment_id)), NOW)

    messages = await _claim(uow_factory)

    assert len(messages) == 1
    assert messages[0].chat_id == int(guest.external_id)
    assert messages[0].text == "Напоминание: через 5 мин «Запись к стилисту» в 13:00."


async def test_auto_cancel_text_uses_moscow_time(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    clean_booking(sync_engine)
    guest = create_guest(sync_engine, external_id="7002", now=NOW)
    booked = await book_as(
        uow_factory, guest, BRAND_C_SLUG, make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    )
    async with uow_factory() as uow:
        await mark_auto_cancelled(uow.repositories, AppointmentId(int(booked.appointment_id)), NOW)

    messages = await _claim(uow_factory)

    assert [m.text for m in messages] == ["Отменено: «Запись к стилисту» на 13:00."]


async def test_leaflet_text_contains_absolute_url_from_limits(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Листовка: заголовок и ссылка из limits, база - PUBLIC_BASE_URL (решение п.5, п.11)."""
    clean_booking(sync_engine)
    guest = create_guest(sync_engine, external_id="7003", now=NOW)
    booked = await book_as(
        uow_factory, guest, BRAND_C_SLUG, make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    )
    admin_id = create_booking_admin(sync_engine)
    grant_brand(sync_engine, admin_id, "brand_c")
    await arrive_as(
        uow_factory,
        int(booked.appointment_id),
        admin_id,
        frozenset({brand_id(sync_engine, "brand_c")}),
    )

    messages = await _claim(uow_factory)

    assert [m.text for m in messages] == [
        "Листовка «Листовка Бренд C»: https://test.example/static/leaflets/brand_c.pdf"
    ]
    assert appointment_row(sync_engine, int(booked.appointment_id)).status == "arrived"
