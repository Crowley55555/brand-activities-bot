from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.admin_repo import SqlAdminRepo
from app.db.repositories.attempts_repo import SqlAttemptRepo
from app.db.repositories.audit_repo import SqlAuditRepo
from app.db.repositories.booking_repo import SqlAppointmentRepo, SqlSlotRepo
from app.db.repositories.catalog_repo import SqlCatalogRepo
from app.db.repositories.channels_repo import SqlChannelAccountRepo
from app.db.repositories.consent_repo import SqlConsentRepo
from app.db.repositories.frame_repo import SqlFrameRepo
from app.db.repositories.jobs_repo import SqlJobRepo
from app.db.repositories.media_repo import SqlMediaRepo
from app.db.repositories.metrics_repo import SqlMetricsRepo
from app.db.repositories.moderation_repo import SqlModerationRepo
from app.db.repositories.rewards_repo import SqlRewardRepo
from app.db.repositories.survey_repo import SqlSurveyAnswerRepo
from app.db.repositories.tg_fsm_repo import SqlTgFsmStateRepo
from app.db.repositories.ugc_repo import SqlUgcRepo
from app.db.repositories.users_repo import SqlUserRepo
from app.db.repositories.wheel_repo import SqlWheelRepo

if TYPE_CHECKING:
    from app.db.unit_of_work import Repositories


def build_all(session: AsyncSession) -> Repositories:
    from app.db.unit_of_work import Repositories

    return Repositories(
        users=SqlUserRepo(session),
        channels=SqlChannelAccountRepo(session),
        consents=SqlConsentRepo(session),
        catalog=SqlCatalogRepo(session),
        admins=SqlAdminRepo(session),
        attempts=SqlAttemptRepo(session),
        rewards=SqlRewardRepo(session),
        moderation=SqlModerationRepo(session),
        media=SqlMediaRepo(session),
        surveys=SqlSurveyAnswerRepo(session),
        ugc=SqlUgcRepo(session),
        jobs=SqlJobRepo(session),
        slots=SqlSlotRepo(session),
        appointments=SqlAppointmentRepo(session),
        audit=SqlAuditRepo(session),
        wheel=SqlWheelRepo(session),
        frames=SqlFrameRepo(session),
        metrics=SqlMetricsRepo(session),
        tg_fsm=SqlTgFsmStateRepo(session),
    )


__all__ = [
    "SqlFrameRepo",
    "SqlWheelRepo",
    "SqlAdminRepo",
    "SqlAppointmentRepo",
    "SqlAttemptRepo",
    "SqlAuditRepo",
    "SqlSlotRepo",
    "SqlCatalogRepo",
    "SqlChannelAccountRepo",
    "SqlConsentRepo",
    "SqlJobRepo",
    "SqlMediaRepo",
    "SqlMetricsRepo",
    "SqlModerationRepo",
    "SqlRewardRepo",
    "SqlSurveyAnswerRepo",
    "SqlTgFsmStateRepo",
    "SqlUgcRepo",
    "SqlUserRepo",
    "build_all",
]
