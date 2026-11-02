"""Фабрики фазы 5 (Бренд D): активность колеса/рамки/подтверждения и призы в тестовой БД.

Призы и активности создаёт фабрика со случайным slug: сиды миграции 0014 тесты не меняют,
потому что clean_db не чистит wheel_prizes и остатки stock_left протекли бы в соседние тесты.
Чтение строк - прямым SQL, как в tests/factories/ugc.py: баг в ядре не маскируется его же функцией.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

WHEEL_LIMITS: dict[str, Any] = {"max_per_day": 1, "wheel_prizes_mode": "random_weighted"}
PHOTO_LIMITS: dict[str, Any] = {
    "max_per_day": 1,
    "frame_slug": "brand_c_polaroid",
    "max_file_bytes": 10485760,
    "allowed_mimes": ["image/jpeg", "image/png"],
    "system_reject_reasons": {"polaroid_render_failed": "Не удалось обработать фото"},
}
CONFIRMATION_LIMITS: dict[str, Any] = {
    "max_per_day": 1,
    "requires_moderation": True,
    "reject_reasons": {
        "invalid_link": "Неверная ссылка",
        "not_posted": "Пост не опубликован",
        "other": "Другое",
    },
    "promocode_template": "BRAND_D-{random8}",
}


@dataclass(frozen=True)
class BrandDActivity:
    slug: str
    activity_id: int


def make_activity(engine: Engine, kind: str, limits: dict[str, Any]) -> BrandDActivity:
    slug = f"{kind}-test-{uuid.uuid4().hex[:10]}"
    with engine.begin() as conn:
        activity_id = conn.execute(
            text(
                "INSERT INTO activities (brand_id, slug, display_name, kind, is_active, limits, "
                "created_at, updated_at) "
                "SELECT b.id, :slug, :slug, :kind, TRUE, CAST(:limits AS jsonb), now(), now() "
                "FROM brands b WHERE b.slug = 'brand_d' RETURNING id"
            ),
            {"slug": slug, "kind": kind, "limits": _as_json(limits)},
        ).scalar_one()
    return BrandDActivity(slug=slug, activity_id=int(activity_id))


def make_prize(
    engine: Engine,
    activity_id: int,
    title: str,
    prize_kind: str,
    weight: int,
    stock_left: int | None = None,
    code_template: str | None = None,
) -> int:
    with engine.begin() as conn:
        prize_id = conn.execute(
            text(
                "INSERT INTO wheel_prizes (activity_id, title, prize_kind, weight, stock_left, "
                "code_template, created_at, updated_at) "
                "VALUES (:aid, :title, :kind, :weight, :stock, :tpl, now(), now()) RETURNING id"
            ),
            {
                "aid": activity_id,
                "title": title,
                "kind": prize_kind,
                "weight": weight,
                "stock": stock_left,
                "tpl": code_template,
            },
        ).scalar_one()
    return int(prize_id)


def prize_stock(engine: Engine, prize_id: int) -> int | None:
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT stock_left FROM wheel_prizes WHERE id = :id"), {"id": prize_id}
        ).scalar_one()
    return None if value is None else int(value)


def wheel_spin_rows(engine: Engine, user_id: int) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT * FROM wheel_spins WHERE user_id = :uid ORDER BY id"),
                {"uid": user_id},
            ).all()
        )


def frame_rows(engine: Engine, attempt_id: int) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT * FROM frame_submissions WHERE attempt_id = :aid ORDER BY id"),
                {"aid": attempt_id},
            ).all()
        )


def attempt_status(engine: Engine, attempt_id: int) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT status FROM attempts WHERE id = :id"), {"id": attempt_id}
            ).scalar_one()
        )


def _as_json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
