"""Порт рамки для фото (ADR-41): ядро не импортирует Pillow (правило 3).

Реализация - app/adapters/image_frame/pillow.py. Ошибки входа объявлены здесь, чтобы ядро и
воркер ловили их, не импортируя адаптер.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class ImageTooLargeError(Exception):
    """Файл больше 10 MB или разрешение больше 4000x4000 (ADR-41)."""


class ImageDecodeError(Exception):
    """Байты не декодируются как изображение (битый или неподдерживаемый формат)."""


@runtime_checkable
class ImageFramePort(Protocol):
    def apply(self, src_bytes: bytes, frame_slug: str) -> bytes:
        """Наложить рамку `frame_slug` на фото и вернуть JPEG."""
        ...
