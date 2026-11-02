from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from app.core.admin_access.repository import AdminRepo
    from app.db.repositories.attempts_repo import SqlAttemptRepo
    from app.db.repositories.audit_repo import SqlAuditRepo
    from app.db.repositories.booking_repo import SqlAppointmentRepo, SqlSlotRepo
    from app.db.repositories.catalog_repo import CatalogRepo
    from app.db.repositories.channels_repo import ChannelAccountRepo
    from app.db.repositories.consent_repo import ConsentRepo
    from app.db.repositories.frame_repo import SqlFrameRepo
    from app.db.repositories.jobs_repo import SqlJobRepo
    from app.db.repositories.media_repo import SqlMediaRepo
    from app.db.repositories.metrics_repo import SqlMetricsRepo
    from app.db.repositories.moderation_repo import SqlModerationRepo
    from app.db.repositories.rewards_repo import SqlRewardRepo
    from app.db.repositories.survey_repo import SqlSurveyAnswerRepo
    from app.db.repositories.tg_fsm_repo import SqlTgFsmStateRepo
    from app.db.repositories.ugc_repo import SqlUgcRepo
    from app.db.repositories.users_repo import UserRepo
    from app.db.repositories.wheel_repo import SqlWheelRepo


@dataclass
class Repositories:
    users: UserRepo
    channels: ChannelAccountRepo
    consents: ConsentRepo
    catalog: CatalogRepo
    admins: AdminRepo
    attempts: SqlAttemptRepo
    rewards: SqlRewardRepo
    moderation: SqlModerationRepo
    media: SqlMediaRepo
    surveys: SqlSurveyAnswerRepo
    ugc: SqlUgcRepo
    jobs: SqlJobRepo
    slots: SqlSlotRepo
    appointments: SqlAppointmentRepo
    audit: SqlAuditRepo
    wheel: SqlWheelRepo
    frames: SqlFrameRepo
    metrics: SqlMetricsRepo
    tg_fsm: SqlTgFsmStateRepo


class UnitOfWork:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._session: AsyncSession | None = None
        self._repositories: Repositories | None = None

    async def __aenter__(self) -> UnitOfWork:
        self._session = self._factory()
        from app.db.repositories import build_all

        self._repositories = build_all(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._session is not None
        try:
            if exc is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()
            self._session = None
            self._repositories = None

    async def commit(self) -> None:
        """Явный commit до формирования HTTP-ответа (F2, ревью фазы 3a).

        Клиент получает 2xx/4xx только после фиксации записи. Повторный commit в __aexit__
        безопасен: транзакция уже пустая.
        """
        assert self._session is not None, "UoW must be entered"
        await self._session.commit()

    @property
    def session(self) -> AsyncSession:
        assert self._session is not None, "UoW must be entered"
        return self._session

    @property
    def repositories(self) -> Repositories:
        assert self._repositories is not None, "UoW must be entered"
        return self._repositories
