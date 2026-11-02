"""Листовка Бренд C отдаётся статикой приложения (решение п.5): /static/leaflets/brand_c.pdf.

Файл - валидный одностраничный PDF (заглушка до макета дизайнера). Публичный, без ПДн.
"""

from __future__ import annotations

from httpx import AsyncClient


async def test_leaflet_pdf_is_served(api_client: AsyncClient) -> None:
    resp = await api_client.get("/static/leaflets/brand_c.pdf")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content.startswith(b"%PDF-1.4")
    assert resp.content.rstrip().endswith(b"%%EOF")
