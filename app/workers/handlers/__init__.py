"""Реестр обработчиков воркера: kind -> handler и kind -> on-dead хук (ADR-28, ADR-30, ТОП-1/2)."""

from __future__ import annotations

from typing import Final

from app.workers.context import Handler, OnDead
from app.workers.handlers import (
    auto_cancel_appointment,
    ingest_ugc_file,
    notify_appointment_reminder,
    notify_confirmation_rejected,
    notify_ingest_failed,
    notify_polaroid_ready,
    notify_promocode,
    notify_reward,
    notify_ugc_rejected,
    notify_wheel_result,
    polaroid_render,
    s3_delete,
)

HANDLERS: Final[dict[str, Handler]] = {
    "ingest_ugc_file": ingest_ugc_file.run,
    "notify_reward": notify_reward.run,
    "notify_ugc_rejected": notify_ugc_rejected.run,
    "notify_ingest_failed": notify_ingest_failed.run,
    "s3_delete": s3_delete.run,
    "notify_appointment_reminder": notify_appointment_reminder.run,
    "auto_cancel_appointment": auto_cancel_appointment.run,
    "polaroid_render": polaroid_render.run,
    "notify_wheel_result": notify_wheel_result.run,
    "notify_polaroid_ready": notify_polaroid_ready.run,
    "notify_promocode": notify_promocode.run,
    "notify_confirmation_rejected": notify_confirmation_rejected.run,
}

KIND_ON_DEAD: Final[dict[str, OnDead]] = {
    "ingest_ugc_file": ingest_ugc_file.on_ingest_dead,
    "polaroid_render": polaroid_render.on_dead,
}

__all__ = ["HANDLERS", "KIND_ON_DEAD"]
