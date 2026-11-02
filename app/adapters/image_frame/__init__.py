"""Адаптер рамки Polaroid (ADR-41): build(settings) отдаёт ImageFramePort на Pillow.

Холст и лимиты зашиты в адаптер (U10): они не зависят от настроек окружения.
"""

from __future__ import annotations

from app.adapters.image_frame.pillow import PillowImageFrame
from app.config import Settings


def build(settings: Settings) -> PillowImageFrame:
    return PillowImageFrame()


__all__ = ["PillowImageFrame", "build"]
