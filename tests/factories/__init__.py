"""Фабрики тестовых данных (синхронный SQL мимо ядра).

Тесты ядра не должны создавать гостей теми же функциями, которые проверяют:
иначе баг в `core.users`/`core.consent` проявится в десятке несвязанных тестов.
Поэтому фабрики пишут строки прямо в таблицы и возвращают только id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.consent.policy import CURRENT_POLICY_TEXT_HASH, CURRENT_POLICY_VERSION

DEFAULT_NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Guest:
    user_id: int
    channel_account_id: int
    external_id: str


def create_guest(
    engine: Engine,
    external_id: str = "1001",
    *,
    with_consent: bool = True,
    now: datetime = DEFAULT_NOW,
) -> Guest:
    """Гость + telegram-привязка (+ действующее согласие текущей версии)."""
    with engine.begin() as conn:
        user_id = conn.execute(
            text(
                "INSERT INTO users (created_at, updated_at) "
                "VALUES (:ts, :ts) RETURNING id"
            ),
            {"ts": now},
        ).scalar_one()
        channel_id = conn.execute(
            text(
                "INSERT INTO channel_accounts "
                "(user_id, channel_type, external_id, linked_at, created_at, updated_at) "
                "VALUES (:uid, 'telegram', :eid, :ts, :ts, :ts) RETURNING id"
            ),
            {"uid": user_id, "eid": external_id, "ts": now},
        ).scalar_one()
        if with_consent:
            conn.execute(
                text(
                    "INSERT INTO consents "
                    "(user_id, policy_version, text_hash, accepted_at, channel_type, "
                    " created_at, updated_at) "
                    "VALUES (:uid, :ver, :h, :ts, 'telegram', :ts, :ts)"
                ),
                {
                    "uid": user_id,
                    "ver": CURRENT_POLICY_VERSION,
                    "h": CURRENT_POLICY_TEXT_HASH,
                    "ts": now,
                },
            )
    return Guest(
        user_id=int(user_id),
        channel_account_id=int(channel_id),
        external_id=external_id,
    )


def revoke_consent(engine: Engine, guest: Guest, now: datetime = DEFAULT_NOW) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE consents SET revoked_at = :ts WHERE user_id = :uid"),
            {"ts": now, "uid": guest.user_id},
        )


def create_attempt(
    engine: Engine,
    guest: Guest,
    activity_slug: str,
    *,
    status: str = "completed",
    started_at: datetime = DEFAULT_NOW,
) -> int:
    """Готовая строка attempts (для тестов, которым не нужен весь опрос)."""
    finished = None if status == "in_progress" else started_at
    with engine.begin() as conn:
        attempt_id = conn.execute(
            text(
                "INSERT INTO attempts "
                "(user_id, activity_id, channel_account_id, status, started_at, "
                " finished_at, attempt_date, created_at, updated_at) "
                "SELECT :uid, a.id, :cid, :status, CAST(:ts AS timestamptz), "
                " CAST(:fin AS timestamptz), "
                " (CAST(:ts AS timestamptz) AT TIME ZONE 'Europe/Moscow')::date, "
                " CAST(:ts AS timestamptz), CAST(:ts AS timestamptz) "
                "FROM activities a WHERE a.slug = :slug "
                "RETURNING id"
            ),
            {
                "uid": guest.user_id,
                "cid": guest.channel_account_id,
                "status": status,
                "ts": started_at,
                "fin": finished,
                "slug": activity_slug,
            },
        ).scalar_one()
    return int(attempt_id)
