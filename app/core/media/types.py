"""Типы раздачи фото UGC (фаза 3b, П2): адрес файла подачи и байты для ответа.

Адрес объекта (бакет и ключ) живёт только внутри ядра и адаптера хранилища. Гостю и админке
бакет не показываем (правило 9): наружу уходит только ссылка /media/<submission_id>?t=<token>.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.types.ids import UgcSubmissionId


@dataclass(frozen=True)
class SubmissionFileRef:
    """Файл подачи в хранилище: объект и MIME, записанный при ingest."""

    submission_id: UgcSubmissionId
    bucket: str
    key: str
    content_type: str


@dataclass(frozen=True)
class MediaBytes:
    """Байты фото и MIME для заголовка Content-Type."""

    data: bytes
    content_type: str
