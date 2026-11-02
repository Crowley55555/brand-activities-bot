from __future__ import annotations

from typing import Literal

ChannelType = Literal["telegram", "max", "whatsapp", "web"]

# Значения совпадают с CHECK-ограничениями alembic/versions/0001_initial.py.
ActivityKind = Literal["survey", "booking", "ugc", "wheel", "photo_frame", "confirmation"]
AttemptStatus = Literal[
    "in_progress",
    "pending_moderation",
    "approved",
    "rejected",
    "cancelled",
    "completed",
    "expired",
]
RewardType = Literal[
    "promocode",
    "voucher",
    "physical_gift",
    "recommendation_set",
    "leaflet",
    "wheel_prize",
]
QuestionType = Literal["single_choice", "multi_choice", "free_text"]
# Значения совпадают с CHECK ugc_status из alembic/versions/0006_ugc_jobs_schema.py.
UgcStatus = Literal["receiving", "pending", "approved", "rejected", "ingest_failed"]
