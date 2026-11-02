from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

# Общие фикстуры (pg_url, sync_engine, manual_clock, api_client) перенесены
# в tests/conftest.py. Здесь остаются только имена, которые импортируют
# существующие тесты (`from tests.api.conftest import TEST_HMAC, ManualClock`),
# и очистка таблиц, специфичная для tests/api.
from tests.conftest import (  # noqa: F401
    PG_CONTAINER,
    TEST_DB_URL,
    TEST_HMAC,
    ManualClock,
)


@pytest.fixture(autouse=True)
def _truncate(sync_engine: Engine) -> Iterator[None]:
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE channel_accounts, consents, users, jobs "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield
