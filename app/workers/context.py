"""Контекст обработчиков воркера (ADR-28): порты и часы, которые собирает __main__ или тест."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.jobs.types import JobRecord
from app.core.ports.clock import ClockPort
from app.core.ports.file_sniffer import FileSnifferPort
from app.core.ports.image_frame import ImageFramePort
from app.core.ports.object_storage import ObjectStoragePort
from app.core.ports.telegram_files import TelegramFilesPort
from app.db.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class WorkerDeps:
    files: TelegramFilesPort
    storage: ObjectStoragePort
    sniffer: FileSnifferPort
    clock: ClockPort
    ugc_bucket: str
    # Фаза 5: бакет результатов Polaroid (U9) и рамка; без рамки polaroid_render падает как
    # неконфиг.
    frames_bucket: str = "frames"
    image_frame: ImageFramePort | None = None


Handler = Callable[[JobRecord, UnitOfWork, WorkerDeps], Awaitable[None]]
OnDead = Callable[[JobRecord, UnitOfWork, WorkerDeps], Awaitable[None]]
