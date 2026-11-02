"""Handler notify_appointment_reminder (ADR-34): напоминание за 5 минут до слота.

Вся логика - в core.activities.booking.mark_reminded (статус, too_late, has_reminders, сообщение
в той же транзакции). Здесь только маппинг исхода на ack: не-reminded - терминальная задача
(бизнес-исход, без повторов), как для notify_reward (TerminalBusinessError).
"""

from __future__ import annotations

from app.core.activities.booking import mark_reminded
from app.core.jobs.types import JobRecord, TerminalBusinessError
from app.core.types.ids import AppointmentId
from app.db.unit_of_work import UnitOfWork
from app.workers.context import WorkerDeps


async def run(job: JobRecord, uow: UnitOfWork, deps: WorkerDeps) -> None:
    appointment_id = AppointmentId(int(job.payload["appointment_id"]))
    outcome = await mark_reminded(uow.repositories, appointment_id, deps.clock.now())
    if outcome != "reminded":
        raise TerminalBusinessError(outcome)
