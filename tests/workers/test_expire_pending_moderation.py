"""C-3 (фаза 10a): джоб expire_pending_moderation закрывает пропущенные подачи.

Канон 03 §382 (ADR-20): без TTL модератор обязан принять решение; если ушёл со стойки — гость
теряет попытки. Джоб переводит pending_moderation в `cancelled` по
`activities.limits.pending_moderation_ttl_minutes` (миграция 0017) и ставит
`notify_ingest_failed` с системной причиной `pending_moderation_expired`.

Активности без TTL (booking, survey без модерации, wheel, photo_frame) джоб не трогает: fail-closed
при отсутствии ключа.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.workers.context import WorkerDeps
from app.workers.scheduler_tick import scheduler_tick
from tests.conftest import ManualClock
from tests.factories import create_guest

NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
UGC_SLUG = "brand_b_photo_subscription"
PHOTO_UGC_SLUG = "brand_e_photo"
CONFIRMATION_SLUG = "brand_d_confirmation"
SURVEY_SLUG = "brand_a_skin_survey"
BOOKING_SLUG = "brand_c_stylist_booking"


def _deps() -> WorkerDeps:
    return WorkerDeps(
        files=None,  # type: ignore[arg-type]
        storage=None,  # type: ignore[arg-type]
        sniffer=None,  # type: ignore[arg-type]
        clock=ManualClock(NOW),
        ugc_bucket="ugc-test",
    )


def _insert_attempt(
    engine: Engine,
    user_id: int,
    channel_id: int,
    activity_slug: str,
    status: str,
    updated_at: datetime,
    started_at: datetime,
) -> int:
    """Прохождение с явно заданным updated_at — для тестов TTL сканера."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO attempts "
                "(user_id, activity_id, channel_account_id, status, started_at, "
                " attempt_date, created_at, updated_at) "
                "SELECT :uid, a.id, :cid, :status, :ts, "
                " (CAST(:ts AS timestamptz) AT TIME ZONE 'Europe/Moscow')::date, "
                " :ts, :upd "
                "FROM activities a WHERE a.slug = :slug "
                "RETURNING id"
            ),
            {
                "uid": user_id,
                "cid": channel_id,
                "status": status,
                "ts": started_at,
                "upd": updated_at,
                "slug": activity_slug,
            },
        ).scalar_one()
    return int(row)


def _status_and_summary(engine: Engine, attempt_id: int) -> tuple[str, str | None]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, summary FROM attempts WHERE id = :id"),
            {"id": attempt_id},
        ).one()
    return str(row.status), row.summary


def _notify_jobs(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT id, status, payload, idempotency_key "
                    "FROM jobs WHERE kind = 'notify_ingest_failed' ORDER BY id"
                )
            ).all()
        )


async def test_pending_moderation_expires_for_ugc(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Бренд B (UGC) с updated_at = NOW - 200 min и TTL 180 → cancelled + notify."""
    guest = create_guest(sync_engine, external_id="8101", now=NOW)
    attempt_id = _insert_attempt(
        sync_engine,
        guest.user_id,
        guest.channel_account_id,
        UGC_SLUG,
        "pending_moderation",
        NOW - timedelta(minutes=200),
        NOW - timedelta(minutes=200),
    )

    await scheduler_tick(uow_factory, NOW, _deps())

    status, summary = _status_and_summary(sync_engine, attempt_id)
    assert status == "cancelled"
    assert summary == "pending_moderation_expired"

    notify = _notify_jobs(sync_engine)
    assert len(notify) == 1
    payload = notify[0].payload
    assert payload["attempt_id"] == attempt_id
    assert payload["system_reason_slug"] == "pending_moderation_expired"
    assert payload["attempt_closed"] is True
    assert payload["activity_slug"] == UGC_SLUG
    assert notify[0].idempotency_key == f"pending_moderation_expired:{attempt_id}"


async def test_pending_moderation_within_ttl_untouched(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """UGC с updated_at = NOW - 100 min (TTL 180) не трогается."""
    guest = create_guest(sync_engine, external_id="8102", now=NOW)
    attempt_id = _insert_attempt(
        sync_engine,
        guest.user_id,
        guest.channel_account_id,
        UGC_SLUG,
        "pending_moderation",
        NOW - timedelta(minutes=100),
        NOW - timedelta(minutes=100),
    )

    await scheduler_tick(uow_factory, NOW, _deps())

    status, _ = _status_and_summary(sync_engine, attempt_id)
    assert status == "pending_moderation"
    assert len(_notify_jobs(sync_engine)) == 0


async def test_booking_not_affected(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Booking без pending_moderation_ttl_minutes — строки pending_moderation не трогает."""
    guest = create_guest(sync_engine, external_id="8103", now=NOW)
    # booking через pending_moderation не ходит, но стороннюю строку туда никто не ставит.
    # Если бы вдруг появилась — fail-closed: без TTL джоб её не закрывает.
    attempt_id = _insert_attempt(
        sync_engine,
        guest.user_id,
        guest.channel_account_id,
        BOOKING_SLUG,
        "pending_moderation",
        NOW - timedelta(hours=10),
        NOW - timedelta(hours=10),
    )

    await scheduler_tick(uow_factory, NOW, _deps())

    status, _ = _status_and_summary(sync_engine, attempt_id)
    assert status == "pending_moderation"
    assert len(_notify_jobs(sync_engine)) == 0


async def test_survey_without_ttl_untouched(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Бренд A-опрос без pending_moderation_ttl_minutes — fail-closed."""
    guest = create_guest(sync_engine, external_id="8104", now=NOW)
    attempt_id = _insert_attempt(
        sync_engine,
        guest.user_id,
        guest.channel_account_id,
        SURVEY_SLUG,
        "pending_moderation",
        NOW - timedelta(hours=10),
        NOW - timedelta(hours=10),
    )

    await scheduler_tick(uow_factory, NOW, _deps())

    status, _ = _status_and_summary(sync_engine, attempt_id)
    assert status == "pending_moderation"
    assert len(_notify_jobs(sync_engine)) == 0


async def test_brand_e_photo_and_brand_d_confirmation_also_expire(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Миграция 0017 даёт TTL всем трём UGC — brand_e_photo и brand_d_confirmation тоже."""
    guest_a = create_guest(sync_engine, external_id="8105", now=NOW)
    guest_b = create_guest(sync_engine, external_id="8106", now=NOW)
    brand_e_attempt = _insert_attempt(
        sync_engine,
        guest_a.user_id,
        guest_a.channel_account_id,
        PHOTO_UGC_SLUG,
        "pending_moderation",
        NOW - timedelta(minutes=200),
        NOW - timedelta(minutes=200),
    )
    brand_d_attempt = _insert_attempt(
        sync_engine,
        guest_b.user_id,
        guest_b.channel_account_id,
        CONFIRMATION_SLUG,
        "pending_moderation",
        NOW - timedelta(minutes=200),
        NOW - timedelta(minutes=200),
    )

    await scheduler_tick(uow_factory, NOW, _deps())

    for aid in (brand_e_attempt, brand_d_attempt):
        status, summary = _status_and_summary(sync_engine, aid)
        assert status == "cancelled"
        assert summary == "pending_moderation_expired"

    notify = _notify_jobs(sync_engine)
    assert len(notify) == 2
    activity_slugs = {job.payload["activity_slug"] for job in notify}
    assert activity_slugs == {PHOTO_UGC_SLUG, CONFIRMATION_SLUG}
