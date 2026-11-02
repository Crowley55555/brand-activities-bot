"""Рамка Polaroid на Pillow (ADR-41): фото cover-кропом на холст, поверх overlay RGBA, JPEG 85.

Холст фиксирован 1200x1600 (3:4, решение U10): overlay растягивается до холста, поэтому размер
placeholder-PNG не задаёт размер результата. Лимиты входа: 10 MB и 4000x4000 - ImageTooLargeError,
битые байты - ImageDecodeError. Overlay-файлы лежат в app/static/frames/<frame_slug>.png.
"""

from __future__ import annotations

import io
import re
from functools import lru_cache
from pathlib import Path
from typing import Final

from PIL import Image, UnidentifiedImageError

from app.core.ports.image_frame import ImageDecodeError, ImageTooLargeError

CANVAS_SIZE: Final[tuple[int, int]] = (1200, 1600)
MAX_SOURCE_BYTES: Final[int] = 10 * 1024 * 1024
MAX_SOURCE_SIDE: Final[int] = 4000
JPEG_QUALITY: Final[int] = 85
FRAMES_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "static" / "frames"
_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9_]{1,64}")


@lru_cache(maxsize=16)
def _overlay(frame_slug: str) -> Image.Image:
    """Overlay рамки на холст. Файл читаем один раз; сам объект не меняем (только композиция)."""
    if not _SLUG_RE.fullmatch(frame_slug):
        raise ValueError("frame_slug_invalid")
    try:
        raw = Image.open(FRAMES_DIR / f"{frame_slug}.png")
    except FileNotFoundError as exc:
        # Ассет рамки не попал в сборку (например, не закоммичен): деградируем в ошибку рендера,
        # воркер закроет подачу и уведомит гостя, а не упадёт в повторы.
        raise ImageDecodeError("overlay_missing") from exc
    with raw:
        return raw.convert("RGBA").resize(CANVAS_SIZE, Image.Resampling.LANCZOS)


class PillowImageFrame:
    def apply(self, src_bytes: bytes, frame_slug: str) -> bytes:
        if len(src_bytes) > MAX_SOURCE_BYTES:
            raise ImageTooLargeError("file_too_large")
        try:
            with Image.open(io.BytesIO(src_bytes)) as probe:
                width, height = probe.size
            if width > MAX_SOURCE_SIDE or height > MAX_SOURCE_SIDE:
                raise ImageTooLargeError("resolution_too_large")
            with Image.open(io.BytesIO(src_bytes)) as source:
                photo = source.convert("RGB")
        except ImageTooLargeError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ImageDecodeError("not_an_image") from exc

        target_width, target_height = CANVAS_SIZE
        scale = max(target_width / photo.width, target_height / photo.height)
        resized = photo.resize(
            (round(photo.width * scale), round(photo.height * scale)),
            Image.Resampling.LANCZOS,
        )
        left = (resized.width - target_width) // 2
        top = (resized.height - target_height) // 2
        canvas = resized.crop((left, top, left + target_width, top + target_height)).convert(
            "RGBA"
        )
        canvas.alpha_composite(_overlay(frame_slug))

        buffer = io.BytesIO()
        canvas.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY)
        return buffer.getvalue()
