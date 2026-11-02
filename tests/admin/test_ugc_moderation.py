"""Приёмочные тесты фазы 3b: /admin/ugc, CSRF app-level, приёмка по декларации (ADR-22..25).

T1-T8 - по промту 2026-10-17 (T7/T8 - по решению Q1: декларированные mime_type и file_size_bytes).
E1-E3 - защита POST модерации: RBAC по бренду, CSRF, причина отказа вне списка сида.
Идём через настоящий Postgres, ASGI-клиент и ядро, без моков. Slug активности brand_b и её limits
- из сид-миграций 0007 и 0010 (правило 5). UGC-активность бренда brand_a создаёт фикстура
`brand_a_ugc` через SQL: сид brand_a (опрос) не меняется.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.responses import Response

from app.adapters.object_storage.local_fs import LocalFsObjectStorage
from app.core.activities.confirmation import accept_confirmation
from app.core.activities.ugc import accept_from_user, ingest_file_done, start_ugc
from app.core.types.ids import AttemptId, ChannelAccountId, UgcSubmissionId, UserId
from tests.admin.conftest import CSRF_COOKIE, CreatedAdmin, sign_in
from tests.api.conftest import TEST_HMAC
from tests.factories import Guest, create_guest
from tests.factories.survey import UowFactory
from tests.factories.ugc import job_rows, reward_rows, ugc_rows

ACTIVITY_BRAND_B = "brand_b_photo_subscription"
INTERNAL_SECRET_ENV = "INTERNAL_API_AUTH_SECRET"
NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
TG_FILE_ID = "AgACAgIAAxkBAAIBADMINFILE"
POST_URL = "https://www.instagram.com/p/example/"
PROFILE_URL = "https://www.instagram.com/example/"
MIME_JPEG = "image/jpeg"
SIZE_OK = 102400
SIZE_OVER_LIMIT = 10485760  # 10 МБ при лимите 5 МБ (сид 0007)
REJECT_REASONS = {
    "low_quality": "Низкое качество фото",
    "no_post_link": "Нет ссылки на пост",
    "no_subscription": "Нет подписки",
    "off_topic": "Фото не по теме",
    "other": "Другое",
}
UGC_TEST_LIMITS: dict[str, Any] = {
    "max_per_day": 1,
    "requires_moderation": True,
    "max_file_bytes": 5242880,
    "allowed_mimes": ["image/jpeg", "image/png", "image/webp"],
    "max_resubmits": 1,
    "voucher_title": "Тестовый ваучер",
    "promocode_template": "BRAND_A-{random8}",
    "voucher_code_template": "VOUCHER-{random8}",
    "reject_reasons": REJECT_REASONS,
}


# ---------------------------------------------------------------- помощники


def _grant_brand(engine: Engine, admin_id: int, brand_slug: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO admin_brands (admin_id, brand_id) "
                "SELECT :a, b.id FROM brands b WHERE b.slug = :s"
            ),
            {"a": admin_id, "s": brand_slug},
        )


def _csrf(client: AsyncClient) -> str:
    value = client.cookies.get(CSRF_COOKIE)
    assert value, "нет CSRF-cookie: сначала sign_in"
    return value


def _auth(body: bytes) -> dict[str, str]:
    sig = hmac.new(TEST_HMAC.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Internal-Auth": sig, "Content-Type": "application/json"}


async def _post_internal(
    client: AsyncClient, path: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    resp = await client.post(path, content=body, headers=_auth(body))
    return resp.status_code, resp.json()


def _accept_body(
    guest: Guest, attempt_id: int, *, mime: str = MIME_JPEG, size: int = SIZE_OK
) -> dict[str, Any]:
    return {
        "channel_type": "telegram",
        "external_id": guest.external_id,
        "attempt_id": attempt_id,
        "tg_file_id": TG_FILE_ID,
        "mime_type": mime,
        "file_size_bytes": size,
        "post_url": POST_URL,
        "subscription_url": PROFILE_URL,
    }


async def _start(uow_factory: UowFactory, guest: Guest, slug: str) -> int:
    async with uow_factory() as uow:
        r = uow.repositories
        started = await start_ugc(
            r.consents,
            r.catalog,
            r.attempts,
            UserId(guest.user_id),
            slug,
            ChannelAccountId(guest.channel_account_id),
            NOW,
        )
    return int(started.attempt_id)


async def _accept(uow_factory: UowFactory, guest: Guest, attempt_id: int) -> int:
    async with uow_factory() as uow:
        r = uow.repositories
        accepted = await accept_from_user(
            r.consents,
            r.catalog,
            r.attempts,
            r.ugc,
            r.jobs,
            UserId(guest.user_id),
            AttemptId(attempt_id),
            TG_FILE_ID,
            MIME_JPEG,
            SIZE_OK,
            POST_URL,
            PROFILE_URL,
            NOW,
        )
    return int(accepted.submission_id)


async def _pending(
    uow_factory: UowFactory, engine: Engine, slug: str, external_id: str
) -> tuple[Guest, int, int]:
    """Гость -> прохождение -> принятое фото -> ingest -> pending. (guest, attempt, submission)."""
    guest = create_guest(engine, external_id)
    attempt_id = await _start(uow_factory, guest, slug)
    submission_id = await _accept(uow_factory, guest, attempt_id)
    async with uow_factory() as uow:
        await ingest_file_done(
            uow.repositories.ugc,
            UgcSubmissionId(submission_id),
            "ugc",
            f"{uuid.uuid4()}.jpg",
            MIME_JPEG,
            SIZE_OK,
        )
    return guest, attempt_id, submission_id


# ---------------------------------------------------------------- фикстуры


@pytest.fixture
def object_storage(tmp_path: Path, api_client: AsyncClient) -> LocalFsObjectStorage:
    """Адаптер локальной папки для превью (lifespan в тестах не идёт, поэтому кладём в state)."""
    from app.main import app

    storage = LocalFsObjectStorage(tmp_path, signing_key=TEST_HMAC)
    app.state.object_storage = storage
    return storage


@pytest.fixture
def brand_a_ugc(sync_engine: Engine, clean_db: None) -> Iterator[str]:
    """UGC-активность бренда brand_a (тестовые данные): для T2 и T5 нужна вторая очередь."""
    slug = f"brand_a_ugc_{uuid.uuid4().hex[:8]}"
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO activities (brand_id, slug, display_name, kind, is_active, limits, "
                "created_at, updated_at) "
                "SELECT b.id, :slug, 'UGC тест', 'ugc', TRUE, CAST(:limits AS jsonb), now(), now() "
                "FROM brands b WHERE b.slug = 'brand_a'"
            ),
            {"slug": slug, "limits": json.dumps(UGC_TEST_LIMITS, ensure_ascii=False)},
        )
    yield slug
    with sync_engine.begin() as conn:
        # Прохождения держат активность (RESTRICT): сначала гости, потом справочник.
        conn.execute(
            text("TRUNCATE TABLE channel_accounts, consents, users, jobs RESTART IDENTITY CASCADE")
        )
        conn.execute(text("DELETE FROM activities WHERE slug = :slug"), {"slug": slug})


@pytest.fixture
def probe_route() -> Iterator[None]:
    """Подроутер, подключённый уже после сборки приложения (T6, ADR-22)."""
    from app.main import app

    probe = APIRouter(prefix="/admin/probe")

    @probe.post("/x", status_code=204)
    async def _probe() -> Response:
        return Response(status_code=204)

    before = list(app.router.routes)
    app.include_router(probe)
    yield
    # FastAPI 0.143 хранит подключённые роутеры как _IncludedRouter без path: откатываем список
    # и сбрасываем кеш эффективных маршрутов (_mark_routes_changed - приватный метод FastAPI).
    app.router.routes[:] = before
    app.router._mark_routes_changed()


# ---------------------------------------------------------------- T1-T2: очередь


async def test_admin_ugc_requires_session(api_client: AsyncClient) -> None:
    """T1: без сессии - редирект на вход с next (правило 10); маршрут не вызывается."""
    resp = await api_client.get("/admin/ugc")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/login?next=/admin/ugc"


async def test_admin_ugc_pending_list_shows_brand_items(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
    brand_a_ugc: str,
    object_storage: LocalFsObjectStorage,
) -> None:
    """T2: в очереди только pending своих брендов (RBAC): 2 brand_a, brand_b - не виден."""
    admin = admin_user_factory()
    _grant_brand(sync_engine, admin.id, "brand_a")
    _, _, own_first = await _pending(uow_factory, sync_engine, brand_a_ugc, "2001")
    _, _, own_second = await _pending(uow_factory, sync_engine, brand_a_ugc, "2002")
    _, _, foreign = await _pending(uow_factory, sync_engine, ACTIVITY_BRAND_B, "2003")
    await sign_in(admin_client, admin)

    resp = await admin_client.get("/admin/ugc")

    assert resp.status_code == 200
    shown = set(re.findall(r'href="/admin/ugc/(\d+)"', resp.text))
    assert shown == {str(own_first), str(own_second)}
    assert str(foreign) not in shown


# ---------------------------------------------------------------- T3-T5: решения и доступ


async def test_admin_approve_grants_rewards_and_enqueues_notify(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
    object_storage: LocalFsObjectStorage,
) -> None:
    """T3: approve -> 302 на очередь; 2 награды (ваучер и промокод); 1 задача notify_reward.

    Payload задачи - reward_id ваучера (решение Q2): воркер 3c по нему найдёт прохождение.
    """
    admin = admin_user_factory()
    _grant_brand(sync_engine, admin.id, "brand_b")
    guest, attempt_id, submission_id = await _pending(
        uow_factory, sync_engine, ACTIVITY_BRAND_B, "3001"
    )
    await sign_in(admin_client, admin)

    resp = await admin_client.post(
        f"/admin/ugc/{submission_id}/approve", data={"_csrf": _csrf(admin_client)}
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/ugc"
    rewards = reward_rows(sync_engine, attempt_id)
    assert sorted(r.reward_type for r in rewards) == ["promocode", "voucher"]
    voucher = next(r for r in rewards if r.reward_type == "voucher")
    notify = job_rows(sync_engine, "notify_reward")
    assert len(notify) == 1
    assert int(notify[0].payload["reward_id"]) == int(voucher.id)


async def test_admin_reject_marks_submission_and_enqueues_notify(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
    object_storage: LocalFsObjectStorage,
) -> None:
    """T4: reject с причиной -> 302; status=rejected; 1 задача notify_ugc_rejected.

    Первый отказ при max_resubmits=1: повтор ещё возможен, attempt_closed=false (Q3).
    """
    admin = admin_user_factory()
    _grant_brand(sync_engine, admin.id, "brand_b")
    guest, attempt_id, submission_id = await _pending(
        uow_factory, sync_engine, ACTIVITY_BRAND_B, "4001"
    )
    await sign_in(admin_client, admin)

    resp = await admin_client.post(
        f"/admin/ugc/{submission_id}/reject",
        data={"_csrf": _csrf(admin_client), "reason_slug": "low_quality", "reason_note": ""},
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/ugc"
    (row,) = ugc_rows(sync_engine, attempt_id)
    assert row.status == "rejected"
    notify = job_rows(sync_engine, "notify_ugc_rejected")
    assert len(notify) == 1
    assert notify[0].payload == {
        "user_id": guest.user_id,
        "reason_slug": "low_quality",
        "attempt_closed": False,
        "attempt_id": attempt_id,
    }


async def test_admin_without_brand_access_returns_403(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
    brand_a_ugc: str,
    object_storage: LocalFsObjectStorage,
) -> None:
    """T5: без admin_brands на brand_a - детальная карточка 403 (не 404: отказ по праву)."""
    admin = admin_user_factory()
    _, _, submission_id = await _pending(uow_factory, sync_engine, brand_a_ugc, "5001")
    await sign_in(admin_client, admin)

    resp = await admin_client.get(f"/admin/ugc/{submission_id}")

    assert resp.status_code == 403
    assert resp.json() == {"error": "brand_access_denied"}


# ---------------------------------------------------------------- T6: CSRF app-level


async def test_post_without_csrf_returns_403_app_level(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    probe_route: None,
) -> None:
    """T6: подроутер /admin/probe, добавленный после сборки app, без _csrf - 403 (F1 фазы 1.5)."""
    admin = admin_user_factory()
    await sign_in(admin_client, admin)

    unsigned_attempt = await admin_client.post("/admin/probe/x", data={"x": "1"})
    assert unsigned_attempt.status_code == 403
    assert unsigned_attempt.json() == {"error": "csrf_invalid"}

    signed_attempt = await admin_client.post("/admin/probe/x", data={"_csrf": _csrf(admin_client)})
    assert signed_attempt.status_code == 204


# ---------------------------------------------------------------- T7-T8: приёмка по декларации


async def test_accept_rejects_non_image_mime(
    api_client: AsyncClient, uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """T7: декларированный mime_type вне activity.limits.allowed_mimes -> 422 mime_not_allowed."""
    guest = create_guest(sync_engine, "7001")
    attempt_id = await _start(uow_factory, guest, ACTIVITY_BRAND_B)

    status, body = await _post_internal(
        api_client,
        "/internal/v1/activities/ugc/accept",
        _accept_body(guest, attempt_id, mime="application/pdf"),
    )

    assert status == 422
    assert body == {"error": "mime_not_allowed"}


async def test_accept_rejects_file_over_5mb(
    api_client: AsyncClient, uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """T8: file_size_bytes больше activity.limits.max_file_bytes -> 422 file_too_large."""
    guest = create_guest(sync_engine, "8001")
    attempt_id = await _start(uow_factory, guest, ACTIVITY_BRAND_B)

    status, body = await _post_internal(
        api_client,
        "/internal/v1/activities/ugc/accept",
        _accept_body(guest, attempt_id, size=SIZE_OVER_LIMIT),
    )

    assert status == 422
    assert body == {"error": "file_too_large"}
    assert ugc_rows(sync_engine, attempt_id) == []


# ---------------------------------------------------------------- E1-E3: защита POST


async def test_approve_without_brand_access_returns_403_and_keeps_pending(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
    brand_a_ugc: str,
    object_storage: LocalFsObjectStorage,
) -> None:
    """E1: RBAC распространяется и на POST: чужой бренд не одобрить даже с верным CSRF."""
    admin = admin_user_factory()
    _, attempt_id, submission_id = await _pending(uow_factory, sync_engine, brand_a_ugc, "9001")
    await sign_in(admin_client, admin)

    resp = await admin_client.post(
        f"/admin/ugc/{submission_id}/approve", data={"_csrf": _csrf(admin_client)}
    )

    assert resp.status_code == 403
    assert resp.json() == {"error": "brand_access_denied"}
    (row,) = ugc_rows(sync_engine, attempt_id)
    assert row.status == "pending"


async def test_approve_without_csrf_returns_403_and_keeps_pending(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
    object_storage: LocalFsObjectStorage,
) -> None:
    """E2: POST модерации без _csrf - 403 csrf_invalid, решения нет."""
    admin = admin_user_factory()
    _grant_brand(sync_engine, admin.id, "brand_b")
    _, attempt_id, submission_id = await _pending(
        uow_factory, sync_engine, ACTIVITY_BRAND_B, "9002"
    )
    await sign_in(admin_client, admin)

    resp = await admin_client.post(f"/admin/ugc/{submission_id}/approve", data={})

    assert resp.status_code == 403
    assert resp.json() == {"error": "csrf_invalid"}
    (row,) = ugc_rows(sync_engine, attempt_id)
    assert row.status == "pending"
    assert reward_rows(sync_engine, attempt_id) == []


async def test_reject_with_unknown_reason_returns_422(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
    object_storage: LocalFsObjectStorage,
) -> None:
    """E3: причина вне activity.limits.reject_reasons - 422 reason_slug_unknown (правило 5)."""
    admin = admin_user_factory()
    _grant_brand(sync_engine, admin.id, "brand_b")
    _, attempt_id, submission_id = await _pending(
        uow_factory, sync_engine, ACTIVITY_BRAND_B, "9003"
    )
    await sign_in(admin_client, admin)

    resp = await admin_client.post(
        f"/admin/ugc/{submission_id}/reject",
        data={"_csrf": _csrf(admin_client), "reason_slug": "made_up_reason", "reason_note": ""},
    )

    assert resp.status_code == 422
    assert resp.json() == {"error": "reason_slug_unknown"}
    (row,) = ugc_rows(sync_engine, attempt_id)
    assert row.status == "pending"


async def test_detail_page_renders_decision_forms_with_csrf_and_reasons(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
    object_storage: LocalFsObjectStorage,
) -> None:
    """Карточка фото: формы approve и reject с _csrf из cookie, причины из activities.limits."""
    admin = admin_user_factory()
    _grant_brand(sync_engine, admin.id, "brand_b")
    _, _, submission_id = await _pending(uow_factory, sync_engine, ACTIVITY_BRAND_B, "3101")
    await sign_in(admin_client, admin)

    resp = await admin_client.get(f"/admin/ugc/{submission_id}")

    assert resp.status_code == 200
    assert f'value="{_csrf(admin_client)}"' in resp.text
    assert f'action="/admin/ugc/{submission_id}/approve"' in resp.text
    assert f'action="/admin/ugc/{submission_id}/reject"' in resp.text
    assert '<option value="low_quality">Низкое качество фото</option>' in resp.text
    assert 'src="/media/' in resp.text, "превью идёт по ссылке адаптера"


def _inject_unsafe_urls(engine: Engine, submission_id: int) -> None:
    """Подмена ссылок SQL-ом мимо API: проверяем фильтр шаблона, не приёмку (замечание 5)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE ugc_submissions SET post_url = :post, subscription_claimed_url = :sub "
                "WHERE id = :sid"
            ),
            {
                "post": "javascript:alert(1)",
                "sub": "data:text/html,<script>alert(1)</script>",
                "sid": submission_id,
            },
        )


async def test_moderation_detail_blocks_javascript_in_href(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
    object_storage: LocalFsObjectStorage,
) -> None:
    """Замечание 5 (defense-in-depth): ссылка с javascript: или data: в карточке не выводится."""
    admin = admin_user_factory()
    _grant_brand(sync_engine, admin.id, "brand_b")
    _, _, submission_id = await _pending(uow_factory, sync_engine, ACTIVITY_BRAND_B, "3201")
    _inject_unsafe_urls(sync_engine, submission_id)
    await sign_in(admin_client, admin)

    resp = await admin_client.get(f"/admin/ugc/{submission_id}")

    assert resp.status_code == 200
    assert "javascript:" not in resp.text
    assert "data:text/html" not in resp.text
    assert "<script>alert" not in resp.text
    assert 'href="javascript' not in resp.text


async def test_moderation_queue_blocks_javascript_in_href(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
    object_storage: LocalFsObjectStorage,
) -> None:
    """Замечание 5: то же в очереди. Строка на месте, опасная ссылка - нет."""
    admin = admin_user_factory()
    _grant_brand(sync_engine, admin.id, "brand_b")
    _, _, submission_id = await _pending(uow_factory, sync_engine, ACTIVITY_BRAND_B, "3202")
    _inject_unsafe_urls(sync_engine, submission_id)
    await sign_in(admin_client, admin)

    resp = await admin_client.get("/admin/ugc")

    assert resp.status_code == 200
    assert f'href="/admin/ugc/{submission_id}"' in resp.text
    assert "javascript:" not in resp.text
    assert "data:text/html" not in resp.text


async def test_admin_pages_render_without_internal_secret(
    admin_client: AsyncClient,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
    object_storage: LocalFsObjectStorage,
    override_settings: Callable[..., None],
) -> None:
    """Замечание f: без INTERNAL_API_AUTH_SECRET страницы работают, превью не выводятся."""
    admin = admin_user_factory()
    _grant_brand(sync_engine, admin.id, "brand_b")
    _, _, submission_id = await _pending(uow_factory, sync_engine, ACTIVITY_BRAND_B, "3301")
    override_settings(**{INTERNAL_SECRET_ENV: ""})
    await sign_in(admin_client, admin)

    queue = await admin_client.get("/admin/ugc")
    detail = await admin_client.get(f"/admin/ugc/{submission_id}")

    assert queue.status_code == 200
    assert detail.status_code == 200
    assert f'href="/admin/ugc/{submission_id}"' in queue.text
    assert "/media/" not in queue.text
    assert "/media/" not in detail.text


# ---------------------------------------------- B-3: подарок за пост в очереди модерации (фаза 5)


async def _pending_confirmation(
    uow_factory: UowFactory, engine: Engine, external_id: str
) -> tuple[int, int]:
    """Подарок за пост (brand_d_confirmation): ссылка принята ядром, подача в статусе pending."""
    guest = create_guest(engine, external_id=external_id, now=NOW)
    async with uow_factory() as uow:
        repos = uow.repositories
        accepted = await accept_confirmation(
            repos.consents,
            repos.catalog,
            repos.attempts,
            repos.ugc,
            UserId(guest.user_id),
            "brand_d_confirmation",
            ChannelAccountId(guest.channel_account_id),
            POST_URL,
            NOW,
        )
    return int(accepted.attempt_id), int(accepted.submission_id)


async def test_confirmation_submission_in_queue(
    admin_client: AsyncClient,
    clean_db: None,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
) -> None:
    """B-3: подарок за пост виден в очереди /admin/ugc бренда brand_d; превью файла не
    выводим."""
    admin = admin_user_factory()
    _grant_brand(sync_engine, admin.id, "brand_d")
    _attempt_id, submission_id = await _pending_confirmation(uow_factory, sync_engine, "3101")
    await sign_in(admin_client, admin)

    resp = await admin_client.get("/admin/ugc")

    assert resp.status_code == 200
    assert f'id="ugc-{submission_id}"' in resp.text
    assert POST_URL in resp.text
    assert 'alt="Фото на модерации"' not in resp.text


async def test_approve_confirmation_from_admin(
    admin_client: AsyncClient,
    clean_db: None,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
) -> None:
    """B-3: approve из админки -> approved, промокод BRAND_D-XXXXXXXX, notify_promocode; 302 на
    очередь."""
    admin = admin_user_factory()
    _grant_brand(sync_engine, admin.id, "brand_d")
    attempt_id, submission_id = await _pending_confirmation(uow_factory, sync_engine, "3102")
    await sign_in(admin_client, admin)

    resp = await admin_client.post(
        f"/admin/ugc/{submission_id}/approve", data={"_csrf": _csrf(admin_client)}
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/ugc"
    assert ugc_rows(sync_engine, attempt_id)[0].status == "approved"
    rewards = reward_rows(sync_engine, attempt_id)
    assert [row.reward_type for row in rewards] == ["promocode"]
    assert re.fullmatch(r"BRAND_D-[A-Z0-9]{8}", rewards[0].code)
    notify = job_rows(sync_engine, "notify_promocode")
    assert [int(job.payload["reward_id"]) for job in notify] == [int(rewards[0].id)]


async def test_reject_confirmation_from_admin(
    admin_client: AsyncClient,
    clean_db: None,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
) -> None:
    """B-3: reject из админки -> rejected, прохождение закрыто сразу, уведомление с причиной."""
    admin = admin_user_factory()
    _grant_brand(sync_engine, admin.id, "brand_d")
    attempt_id, submission_id = await _pending_confirmation(uow_factory, sync_engine, "3104")
    await sign_in(admin_client, admin)

    resp = await admin_client.post(
        f"/admin/ugc/{submission_id}/reject",
        data={"_csrf": _csrf(admin_client), "reason_slug": "not_posted"},
    )

    assert resp.status_code == 302
    assert ugc_rows(sync_engine, attempt_id)[0].status == "rejected"
    with sync_engine.connect() as conn:
        attempt_status = conn.execute(
            text("SELECT status FROM attempts WHERE id = :id"), {"id": attempt_id}
        ).scalar_one()
    assert attempt_status == "rejected"
    notify = job_rows(sync_engine, "notify_confirmation_rejected")
    assert [job.payload["reason_slug"] for job in notify] == ["not_posted"]


async def test_approve_ugc_from_admin_unchanged(
    admin_client: AsyncClient,
    clean_db: None,
    admin_user_factory: Callable[..., CreatedAdmin],
    sync_engine: Engine,
    uow_factory: UowFactory,
    object_storage: LocalFsObjectStorage,
) -> None:
    """Регресс B-3: approve Бренд B UGC из админки по-прежнему даёт ваучер и промокод и
    notify_reward."""
    admin = admin_user_factory()
    _grant_brand(sync_engine, admin.id, "brand_b")
    _guest, attempt_id, submission_id = await _pending(
        uow_factory, sync_engine, ACTIVITY_BRAND_B, "3105"
    )
    await sign_in(admin_client, admin)

    resp = await admin_client.post(
        f"/admin/ugc/{submission_id}/approve", data={"_csrf": _csrf(admin_client)}
    )

    assert resp.status_code == 302
    rewards = reward_rows(sync_engine, attempt_id)
    assert sorted(row.reward_type for row in rewards) == ["promocode", "voucher"]
    assert len(job_rows(sync_engine, "notify_reward")) == 1
    assert job_rows(sync_engine, "notify_promocode") == []
