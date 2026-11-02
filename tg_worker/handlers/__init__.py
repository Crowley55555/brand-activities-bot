from __future__ import annotations

from aiogram import Router

from tg_worker.handlers.activity_survey import router as activity_survey_router
from tg_worker.handlers.booking import router as booking_router
from tg_worker.handlers.confirmation import router as confirmation_router
from tg_worker.handlers.consent import router as consent_router
from tg_worker.handlers.forget import router as forget_router
from tg_worker.handlers.main_menu import router as main_menu_router
from tg_worker.handlers.nav import router as nav_router
from tg_worker.handlers.photo_frame import router as photo_frame_router
from tg_worker.handlers.registration import router as registration_router
from tg_worker.handlers.rewards import router as rewards_router
from tg_worker.handlers.start import router as start_router
from tg_worker.handlers.ugc import router as ugc_router

router = Router()
router.include_router(start_router)
router.include_router(nav_router)
router.include_router(consent_router)
router.include_router(registration_router)
router.include_router(main_menu_router)
router.include_router(activity_survey_router)
router.include_router(booking_router)
router.include_router(photo_frame_router)
router.include_router(confirmation_router)
router.include_router(ugc_router)
# Фаза 10b-1, C-1: /my и callback otp:<id> для именных наград.
router.include_router(rewards_router)
# Фаза 10b-3, I-2: /forget каскад (двойное подтверждение «ПОДТВЕРЖДАЮ»).
router.include_router(forget_router)

__all__ = ["router"]
