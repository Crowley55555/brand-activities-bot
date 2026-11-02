from __future__ import annotations

import logging
import sys

import structlog

from app.common.pii_mask import structlog_pii_processor


def setup_logging(level: int = logging.INFO) -> None:
    """Собираем structlog + stdlib: единый pipeline с PII-маскированием.

    ADR-6 (2026-10-13): правило 2 (логи без ПДн) закрыто процессором
    `structlog_pii_processor` + ответным middleware `pii_mask`.

    structlog пишет через stdlib logging, поэтому стандартные capture-
    механизмы (caplog в pytest, StringIO-handler на root-logger) видят
    итоговый JSON с замаскированными PII.
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            stream=sys.stdout,
            level=level,
            format="%(message)s",
        )
    else:
        root.setLevel(level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog_pii_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
