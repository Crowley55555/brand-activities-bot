"""Handler auto_cancel_appointment (ADR-34): авто-отмена, если гость не пришёл к слоту + 5 минут.

Логика - в core.activities.booking.mark_auto_cancelled (предикат по статусу и arrived_at).
Не-cancelled_auto (пришёл, отменил, уже отменён) - терминальная задача already_resolved.
"""

from __future__ import annotations

from app.core.activities.booking import mark_auto_cancelled
from app.core.jobs.types import JobRecord, TerminalBusinessError
from app.core.types.ids import AppointmentId
from app.db.unit_of_work import UnitOfWork
from app.workers.context import WorkerDeps


async def run(job: JobRecord, uow: UnitOfWork, deps: WorkerDeps) -> None:
    appointment_id = AppointmentId(int(job.payload["appointment_id"]))
    outcome = await mark_auto_cancelled(uow.repositories, appointment_id, deps.clock.now())
    if outcome != "cancelled_auto":
        raise TerminalBusinessError(outcome)
