"""GET /internal/v1/catalog/brands/{slug}/activities отдаёт activities.limits (ADR-45, фаза 6.5).

tg_worker читает из `limits` флаги диалога UGC (`requires_post_url`, `requires_subscription_url`),
которые миграция 0016 кладёт в данные (правило 5); сам в БД адаптер не ходит (правило 3).
Без HMAC-подписи limits не отдаются: внутренний контракт закрыт `X-Internal-Auth`.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from httpx import AsyncClient

from tests.api.conftest import TEST_HMAC

CATALOG_PATH = "/internal/v1/catalog/brands/{brand_slug}/activities"


def _signed_headers() -> dict[str, str]:
    """GET без тела: подпись считается от пустой строки, как в InternalApiClient._get."""
    signature = hmac.new(TEST_HMAC.encode("utf-8"), b"", hashlib.sha256).hexdigest()
    return {"X-Internal-Auth": signature}


async def _activities(api_client: AsyncClient, brand_slug: str) -> dict[str, dict[str, Any]]:
    resp = await api_client.get(
        CATALOG_PATH.format(brand_slug=brand_slug), headers=_signed_headers()
    )
    assert resp.status_code == 200, resp.text
    return {str(item["slug"]): item for item in resp.json()["activities"]}


async def test_ugc_activity_with_links_exposes_flags_and_keeps_old_fields(
    api_client: AsyncClient,
) -> None:
    activities = await _activities(api_client, "brand_b")

    activity = activities["brand_b_photo_subscription"]
    # Прежние поля ответа на месте: tg_worker выбирает активность по slug и kind.
    assert activity["kind"] == "ugc"
    assert activity["display_name"]
    # limits отдаются целиком, флаги диалога положила миграция 0016.
    assert activity["limits"]["requires_post_url"] is True
    assert activity["limits"]["requires_subscription_url"] is True
    assert activity["limits"]["max_resubmits"] == 1


async def test_photo_only_activity_has_false_flags_and_survey_keeps_its_limits(
    api_client: AsyncClient,
) -> None:
    activities = await _activities(api_client, "brand_e")

    photo = activities["brand_e_photo"]
    assert photo["kind"] == "ugc"
    assert photo["limits"]["requires_post_url"] is False
    assert photo["limits"]["requires_subscription_url"] is False
    survey = activities["brand_e_scent_match"]
    assert survey["kind"] == "survey"
    assert survey["limits"]["max_recommendations"] == 3
    assert "requires_post_url" not in survey["limits"]


async def test_limits_are_not_exposed_without_signature(api_client: AsyncClient) -> None:
    resp = await api_client.get(CATALOG_PATH.format(brand_slug="brand_b"))

    assert resp.status_code == 401
    assert "limits" not in resp.text
