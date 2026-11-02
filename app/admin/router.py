from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from starlette.responses import HTMLResponse

from app.admin.auth import router as auth_router
from app.admin.jobs import router as jobs_router
from app.admin.metrics import router as metrics_router
from app.admin.moderation import router as moderation_router
from app.admin.rewards import router as rewards_router
from app.admin.schedule import router as schedule_router
from app.admin.templating import render_page
from app.admin.users import router as users_router
from app.api.deps import current_admin
from app.api.middleware.csrf import attach_csrf_cookie, current_csrf_value
from app.config import Settings, get_settings
from app.core.types.dto import AdminRef

# Правило 10 в трёх слоях:
#   1) AdminSessionMiddleware (app/api/middleware/admin_session.py) пускает на любой /admin/*
#      только с валидной сессией (кроме login, logout и static);
#   2) CSRF double-submit - app-level зависимость csrf_guard (ADR-22), а не этот роутер:
#      она покрывает и подроутеры, подключённые мимо admin_router;
#   3) current_admin в обработчиках - второй рубеж, если middleware когда-нибудь снимут.
# Не добавляйте /admin-маршруты в обход этого роутера (tests/admin/test_csrf.py это проверяет).
router = APIRouter(prefix="/admin", include_in_schema=False)
router.include_router(auth_router)
router.include_router(moderation_router)
router.include_router(schedule_router)
router.include_router(users_router)
router.include_router(rewards_router)
router.include_router(metrics_router)
router.include_router(jobs_router)


@router.get("/")
async def index(
    request: Request,
    admin: Annotated[AdminRef, Depends(current_admin)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    # CSRF-токен нужен nav.html (форма POST /admin/logout, I-3 фазы 10b-2).
    csrf_value, is_new = current_csrf_value(request)
    response = render_page("index.html", title="Главная", admin=admin, csrf=csrf_value)
    if is_new:
        attach_csrf_cookie(response, csrf_value, secure=settings.cookie_secure)
    return response
