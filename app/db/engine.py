from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_engine_from_url(url: str) -> AsyncEngine:
    return create_async_engine(url, future=True, pool_pre_ping=True)
