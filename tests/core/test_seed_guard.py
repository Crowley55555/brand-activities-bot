"""M2 (ревью фазы 4): демо-слоты пишутся только при явном APP_ENV=dev или test (fail-closed)."""

from __future__ import annotations

import pytest

from scripts.seed_demo_slots import ensure_dev_environment


@pytest.mark.parametrize("app_env", ["", "   ", "prod", "production", "staging"])
def test_refuses_without_explicit_dev_environment(app_env: str) -> None:
    with pytest.raises(RuntimeError, match="dev-only"):
        ensure_dev_environment(app_env)


@pytest.mark.parametrize("app_env", ["dev", "test", " DEV "])
def test_allows_explicit_dev_environment(app_env: str) -> None:
    ensure_dev_environment(app_env)
