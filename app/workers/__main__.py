"""Точка входа воркера: python -m app.workers (ADR-28)."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from asyncio import AbstractEventLoop

import httpx

from app.adapters.clock import build as build_clock
from app.adapters.file_sniffer import build as build_sniffer
from app.adapters.image_frame import build as build_image_frame
from app.adapters.object_storage import build as build_object_storage
from app.adapters.telegram_files import build as build_telegram_files
from app.common.logging_setup import setup_logging
from app.config import get_settings
from app.db.engine import create_engine_from_url
from app.db.session import make_session_factory
from app.db.unit_of_work import UnitOfWork
from app.workers.context import WorkerDeps
from app.workers.runner import WorkerRunner
from app.workers.scheduler_tick import run_scheduler


def _install_stop_handlers(loop: AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows: add_signal_handler недоступен; обработчик через signal.signal.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def _run() -> None:
    settings = get_settings()
    engine = create_engine_from_url(settings.database_url)
    session_factory = make_session_factory(engine)

    def uow_factory() -> UnitOfWork:
        return UnitOfWork(session_factory)

    stop = asyncio.Event()
    _install_stop_handlers(asyncio.get_running_loop(), stop)
    clock = build_clock()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            deps = WorkerDeps(
                files=build_telegram_files(settings, client),
                storage=build_object_storage(settings),
                sniffer=build_sniffer(),
                clock=clock,
                ugc_bucket=settings.s3_bucket_ugc,
                frames_bucket=settings.s3_bucket_frames,
                image_frame=build_image_frame(settings),
            )
            scheduler = asyncio.create_task(run_scheduler(uow_factory, deps, stop))
            runner = WorkerRunner(
                uow_factory,
                deps,
                visibility_default_s=settings.visibility_timeout_s_default,
                visibility_by_kind={
                    "ingest_ugc_file": settings.visibility_timeout_s_ingest,
                    "polaroid_render": settings.visibility_timeout_s_ingest,
                },
            )
            try:
                await runner.run_forever(stop)
            finally:
                stop.set()
                await scheduler
    finally:
        await engine.dispose()


def main() -> None:
    setup_logging()
    # httpx логирует полный URL запроса; в URL Telegram Bot API лежит токен бота - не пишем его.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_run())


if __name__ == "__main__":
    main()
