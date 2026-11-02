from __future__ import annotations

import pytest

# Общие фикстуры (pg_url, sync_engine, manual_clock, clean_db, uow_factory)
# живут в tests/conftest.py. Здесь - только то, что нужно всем тестам активностей:
# перед каждым тестом чистим данные гостей и прохождений (справочники остаются).


@pytest.fixture(autouse=True)
def _clean_guest_data(clean_db: None) -> None:
    return None
