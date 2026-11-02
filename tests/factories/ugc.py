"""Фабрики фазы 3a (Бренд B UGC): админ и чтение строк мимо ядра.

Как и остальные фабрики: пишем и читаем строки прямо через SQL, чтобы баг в ядре
не маскировался собственной же функцией создания.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


def _rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


def create_admin(engine: Engine) -> int:
    """Сотрудник бренда для решения модерации (FK ugc_submissions.decided_by_admin_id)."""
    login = f"ugc-{uuid.uuid4().hex[:12]}"
    with engine.begin() as conn:
        admin_id = conn.execute(
            text(
                "INSERT INTO admin_users (login, password_hash, display_name) "
                "VALUES (:login, 'test-hash', 'Тест') RETURNING id"
            ),
            {"login": login},
        ).scalar_one()
    return int(admin_id)


def ugc_rows(engine: Engine, attempt_id: int) -> list[Any]:
    return _rows(
        engine,
        "SELECT * FROM ugc_submissions WHERE attempt_id = :aid ORDER BY id",
        aid=attempt_id,
    )


def job_rows(engine: Engine, kind: str) -> list[Any]:
    return _rows(engine, "SELECT * FROM jobs WHERE kind = :kind ORDER BY id", kind=kind)


def reward_rows(engine: Engine, attempt_id: int) -> list[Any]:
    return _rows(
        engine,
        "SELECT * FROM rewards WHERE attempt_id = :aid ORDER BY id",
        aid=attempt_id,
    )


def attempt_status(engine: Engine, attempt_id: int) -> str:
    rows = _rows(engine, "SELECT status FROM attempts WHERE id = :aid", aid=attempt_id)
    return str(rows[0].status)


def count_rows(engine: Engine, table: str) -> int:
    return int(_rows(engine, f"SELECT count(*) AS n FROM {table}")[0].n)
