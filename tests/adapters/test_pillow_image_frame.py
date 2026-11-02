"""ImageFramePort + Pillow (ADR-41, фаза 5): T11-T13 на настоящем PIL, без сети и БД.

Холст фиксирован 1200x1600 (3:4, решение U10): размер результата не зависит от пикселей
placeholder-PNG рамки.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.adapters.image_frame.pillow import PillowImageFrame
from app.core.ports.image_frame import ImageDecodeError, ImageTooLargeError

CANVAS = (1200, 1600)


def _jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 120, 90)).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def test_apply_jpeg_round_trip() -> None:
    """T11: маленький JPEG -> apply -> валидный JPEG холста 1200x1600."""
    out = PillowImageFrame().apply(_jpeg(300, 400), "brand_c_polaroid")
    image = Image.open(io.BytesIO(out))
    assert image.format == "JPEG"
    assert image.size == CANVAS


def test_apply_file_too_large_raises() -> None:
    """T12: 11 MB -> ImageTooLargeError до декодирования."""
    with pytest.raises(ImageTooLargeError):
        PillowImageFrame().apply(b"\x00" * (11 * 1024 * 1024), "brand_c_polaroid")


def test_apply_resolution_too_large_raises() -> None:
    """T13: 4001x4001 -> ImageTooLargeError (лимит 4000x4000)."""
    with pytest.raises(ImageTooLargeError):
        PillowImageFrame().apply(_jpeg(4001, 4001), "brand_c_polaroid")


def test_missing_overlay_raises_decode_error() -> None:
    """B-2: отсутствующий ассет рамки - ImageDecodeError('overlay_missing'), воркер не падает в
    повторы."""
    with pytest.raises(ImageDecodeError, match="overlay_missing"):
        PillowImageFrame().apply(_jpeg(300, 400), "no_such_frame")
