from __future__ import annotations

from app.core.rewards.code_generator import generate_code
from app.core.rewards.listing import list_rewards_page
from app.core.rewards.otp import (
    NAMED_REWARD_TYPES,
    OTP_LENGTH,
    OtpCard,
    OtpIssued,
    find_by_otp,
    redeem_with_otp,
    request_otp,
)
from app.core.rewards.repository import ActiveNamedReward, RewardRepo
from app.core.rewards.service import grant_on_completion, list_by_user, revoke

__all__ = [
    "ActiveNamedReward",
    "NAMED_REWARD_TYPES",
    "OTP_LENGTH",
    "OtpCard",
    "OtpIssued",
    "RewardRepo",
    "find_by_otp",
    "generate_code",
    "grant_on_completion",
    "list_by_user",
    "list_rewards_page",
    "redeem_with_otp",
    "request_otp",
    "revoke",
]
