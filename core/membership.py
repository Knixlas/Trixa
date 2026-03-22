"""
core/membership.py — Tier logic for Trixa.
Handles tier resolution, feature gating, and message limits.
"""
from __future__ import annotations

from datetime import datetime, timezone

FREE_DAILY_LIMIT = 5

PREMIUM_FEATURES = {
    "attachments",
    "workout_export",
    "structured_plans",
}


def get_user_tier(subscription: dict | None, is_admin: bool) -> str:
    if is_admin:
        return "premium"
    if not subscription:
        return "free"
    status = subscription.get("status", "")
    tier = subscription.get("tier", "free")
    if tier == "premium" and status == "active":
        return "premium"
    if status == "trialing":
        trial_ends = subscription.get("trial_ends_at")
        if trial_ends:
            if isinstance(trial_ends, str):
                try:
                    trial_ends = datetime.fromisoformat(trial_ends.replace("Z", "+00:00"))
                except ValueError:
                    return "free"
            if datetime.now(timezone.utc) < trial_ends:
                return "premium"
    return "free"


def can_send_message(tier: str, daily_count: int) -> bool:
    if tier == "premium":
        return True
    return daily_count < FREE_DAILY_LIMIT


def can_use_feature(tier: str, feature: str) -> bool:
    if tier == "premium":
        return True
    return feature not in PREMIUM_FEATURES


def messages_remaining(tier: str, daily_count: int) -> int | None:
    if tier == "premium":
        return None
    return max(0, FREE_DAILY_LIMIT - daily_count)


def trial_days_remaining(subscription: dict | None) -> int | None:
    if not subscription or subscription.get("status") != "trialing":
        return None
    trial_ends = subscription.get("trial_ends_at")
    if not trial_ends:
        return None
    if isinstance(trial_ends, str):
        try:
            trial_ends = datetime.fromisoformat(trial_ends.replace("Z", "+00:00"))
        except ValueError:
            return None
    return max(0, (trial_ends - datetime.now(timezone.utc)).days)
