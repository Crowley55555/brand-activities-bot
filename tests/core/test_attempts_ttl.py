"""Таймаут протухания прохождения: stale_ttl_minutes из activities.limits (03 §1, 04 §1.3)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.common.errors import ValidationError
from app.core.attempts.service import stale_ttl_for


@pytest.mark.parametrize(
    ("limits", "minutes"),
    [({}, 120), ({"stale_ttl_minutes": 30}, 30)],
)
def test_stale_ttl_defaults_to_120_and_reads_limits(limits: dict[str, Any], minutes: int) -> None:
    assert stale_ttl_for(limits) == timedelta(minutes=minutes)


@pytest.mark.parametrize("value", [0, -5, "30", True, None])
def test_stale_ttl_rejects_bad_config(value: Any) -> None:
    with pytest.raises(ValidationError) as exc:
        stale_ttl_for({"stale_ttl_minutes": value})

    assert exc.value.code == "stale_ttl_invalid"
