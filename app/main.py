from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from starlette.staticfiles import StaticFiles

from app.adapters.clock import build as build_clock
from app.adapters.object_storage import build as build_object_storage
from app.adapters.password import build as build_password_hasher
from app.admin.router import router as admin_router
from app.admin.session import is_secret_usable
from app.admin.templating import STATIC_DIR as ADMIN_STATIC_DIR
from app.api.internal.activities import router as activities_router
from app.api.internal.booking import router as booking_router
from app.api.internal.catalog_activities import router as catalog_activities_router
from app.api.internal.catalog_brands import router as catalog_brands_router
from app.api.internal.channels_tg_fsm import router as channels_tg_fsm_router
from app.api.internal.consent_accept import router as consent_accept_router
from app.api.internal.brand_d import router as brand_d_router
from app.api.internal.rewards import router as rewards_internal_router
from app.api.internal.telegram_outbox import router as telegram_outbox_router
from app.api.internal.telegram_updates import router as telegram_updates_router
from app.api.internal.users_forget import router as users_forget_router
from app.api.internal.users_pii import router as users_pii_router
from app.api.media import router as media_router
from app.api.middleware.admin_session import AdminSessionMiddleware
from app.api.middleware.csrf import csrf_guard
from app.api.middleware.error_handler import register_error_handlers
from app.api.middleware.logging import RequestIdMiddleware
from app.api.middleware.pii_mask import PiiMaskResponseMiddleware
from app.common.logging_setup import setup_logging
from app.config import get_settings
from app.db.engine import create_engine_from_url
from app.db.session import make_session_factory

PUBLIC_STATIC_DIR = Path(__file__).resolve().parent / "static"

logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    engine = create_engine_from_url(settings.database_url)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.clock = build_clock()
    app.state.password_hasher = build_password_hasher()
    # ADR-15: объектное хранилище (dev - local_fs). Эндпоинты фазы 3a его не трогают.
    app.state.object_storage = build_object_storage(settings)
    if not is_secret_usable(settings.admin_session_secret):
        # Один раз при старте; сам /admin/* при этом отвечает 503 (fail-closed).
        logger.warning(
            "admin_session_secret_not_configured",
            extra={"event": "admin_session_secret_not_configured"},
        )
    try:
        yield
    finally:
        await engine.dispose()


# ADR-22: CSRF - app-level зависимость. Покрывает все POST/PUT/PATCH/DELETE приложения,
# включая подроутеры, подключённые мимо admin_router; исключения - в csrf.is_exempt.
app = FastAPI(
    title="brand_activities_bot",
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(csrf_guard)],
)

# Порядок: RequestId → AdminSession → PiiMask → routers.
# Starlette разворачивает middleware обратно (LIFO) — последний add_middleware
# оказывается внешним. PII-mask оборачивает тело ответа ПОСЛЕ того, как
# RequestIdMiddleware поставил заголовок — поэтому Pii добавляется первым
# (внутренний), затем AdminSession (правило 10: auth-периметр /admin/*, его
# редиректы тоже получают X-Request-Id), а RequestId — последним (внешний).
app.add_middleware(PiiMaskResponseMiddleware)
app.add_middleware(AdminSessionMiddleware)
app.add_middleware(RequestIdMiddleware)
register_error_handlers(app)
app.include_router(telegram_updates_router)
app.include_router(users_pii_router)
# Фаза 10b-3 (I-2): /forget каскад (анонимизация + UGC/FSM hard delete), HMAC.
app.include_router(users_forget_router)
app.include_router(consent_accept_router)
app.include_router(catalog_brands_router)
app.include_router(catalog_activities_router)
app.include_router(activities_router)
app.include_router(booking_router)
# Фаза 5 (ADR-39, ADR-40, ADR-42): колесо, Polaroid и подарок за пост, под HMAC.
app.include_router(brand_d_router)
# П2 (фаза 3b): фото UGC по подписанной ссылке /media/<submission_id>?t=<token>.
app.include_router(media_router)
# Фаза 3c (ADR-32): outbox telegram_send для tg_worker, под HMAC.
app.include_router(telegram_outbox_router)
# Фаза 10a (C-4): персистентный FSM-storage для tg_worker, под HMAC (правило 4).
app.include_router(channels_tg_fsm_router)
# Фаза 10b-1 (C-1): OTP-погашение именных наград, под HMAC (правило 4, канон 04 §1.2).
app.include_router(rewards_internal_router)
# Статика админки публична (CSS/JS без данных); AdminSessionMiddleware
# пропускает /admin/static/ без cookie, остальной /admin/* закрыт им же.
app.mount("/admin/static", StaticFiles(directory=ADMIN_STATIC_DIR), name="admin-static")
# Публичная статика: листовки и прочие материалы без ПДн (решение п.5, leaflet_url в limits).
app.mount("/static", StaticFiles(directory=PUBLIC_STATIC_DIR), name="static")
app.include_router(admin_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
