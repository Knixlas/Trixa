"""
core/db.py — Supabase operations for Trixa.
All queries are user_id-scoped.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from supabase import create_client, Client

TRIAL_DAYS = 30


def get_client() -> Client:
    return create_client(
        os.environ.get("SUPABASE_URL", ""),
        os.environ.get("SUPABASE_ANON_KEY", ""),
    )


def get_admin_client() -> Client:
    return create_client(
        os.environ.get("SUPABASE_URL", ""),
        os.environ.get("SUPABASE_SERVICE_KEY", ""),
    )


# ── Auth ─────────────────────────────────────────────────────────

def sign_up(email: str, password: str, name: str = ""):
    client = get_client()
    return client.auth.sign_up({
        "email": email,
        "password": password,
        "options": {"data": {"name": name}},
    })


def sign_in(email: str, password: str):
    client = get_client()
    return client.auth.sign_in_with_password({
        "email": email,
        "password": password,
    })


# ── Profiles ─────────────────────────────────────────────────────

def get_profile(user_id: str, access_token: str) -> dict | None:
    client = get_client()
    client.postgrest.auth(access_token)
    result = client.table("profiles").select("*").eq("id", user_id).execute()
    return result.data[0] if result.data else None


def update_profile(user_id: str, access_token: str, data: dict) -> dict:
    client = get_client()
    client.postgrest.auth(access_token)
    result = client.table("profiles").update(data).eq("id", user_id).execute()
    return result.data[0] if result.data else {}


def profile_to_dict(profile: dict) -> dict:
    """Convert Supabase profile row to a flat dict for system prompt."""
    return {
        "name": profile.get("name", ""),
        "experience_level": profile.get("experience_level", "unknown"),
        "goal": profile.get("goal", ""),
        "weight_kg": float(profile["weight_kg"]) if profile.get("weight_kg") else None,
        "ftp_watts": profile.get("ftp_watts"),
        "at_pace": profile.get("at_pace"),
        "lt_pace": profile.get("lt_pace"),
        "at_hr": profile.get("at_hr"),
        "lt_hr": profile.get("lt_hr"),
        "css": profile.get("css"),
        "ironman_finishes": profile.get("ironman_finishes", 0),
        "next_race_name": profile.get("next_race_name", ""),
        "next_race_date": profile.get("next_race_date", ""),
        "health_notes": profile.get("health_notes", ""),
        "preferences": profile.get("preferences", ""),
    }


# ── Conversations ────────────────────────────────────────────────

def get_conversation(user_id: str, access_token: str) -> dict | None:
    client = get_client()
    client.postgrest.auth(access_token)
    result = (
        client.table("conversations")
        .select("*")
        .eq("user_id", user_id)
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def save_conversation(user_id: str, access_token: str, messages: list, conv_id: str | None = None) -> str:
    client = get_client()
    client.postgrest.auth(access_token)
    if conv_id:
        client.table("conversations").update({"messages": messages}).eq("id", conv_id).execute()
        return conv_id
    result = client.table("conversations").insert({
        "user_id": user_id,
        "messages": messages,
    }).execute()
    return result.data[0]["id"] if result.data else ""


def delete_conversation(user_id: str, access_token: str, conv_id: str):
    client = get_client()
    client.postgrest.auth(access_token)
    client.table("conversations").delete().eq("id", conv_id).eq("user_id", user_id).execute()


# ── Subscriptions ────────────────────────────────────────────────

def get_subscription(user_id: str, access_token: str) -> dict | None:
    client = get_client()
    client.postgrest.auth(access_token)
    result = client.table("subscriptions").select("*").eq("user_id", user_id).execute()
    return result.data[0] if result.data else None


def ensure_trial(user_id: str, access_token: str) -> dict:
    sub = get_subscription(user_id, access_token)
    if sub:
        return sub
    trial_end = (datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)).isoformat()
    admin = get_admin_client()
    result = admin.table("subscriptions").insert({
        "user_id": user_id,
        "tier": "premium",
        "status": "trialing",
        "trial_ends_at": trial_end,
    }).execute()
    return result.data[0] if result.data else {
        "status": "trialing", "tier": "premium", "trial_ends_at": trial_end,
    }


def update_subscription(user_id: str, data: dict):
    admin = get_admin_client()
    existing = admin.table("subscriptions").select("id").eq("user_id", user_id).execute()
    if existing.data:
        admin.table("subscriptions").update(data).eq("user_id", user_id).execute()
    else:
        data["user_id"] = user_id
        admin.table("subscriptions").insert(data).execute()


def set_user_tier(user_id: str, tier: str, status: str = "active"):
    update_subscription(user_id, {"tier": tier, "status": status})


# ── Discount Codes ───────────────────────────────────────────────

def apply_discount_code(user_id: str, code: str) -> tuple[bool, str]:
    admin = get_admin_client()
    result = admin.table("discount_codes").select("*").eq("code", code.upper()).execute()
    if not result.data:
        return False, "Ogiltig rabattkod."
    dc = result.data[0]
    if not dc.get("active", False):
        return False, "Rabattkoden ar inte langre aktiv."
    if dc["times_used"] >= dc["max_uses"]:
        return False, "Rabattkoden har redan anvants max antal ganger."
    if dc["discount_percent"] >= 100:
        update_subscription(user_id, {
            "tier": "premium", "status": "active", "discount_code": code.upper(),
        })
    else:
        update_subscription(user_id, {"discount_code": code.upper()})
    admin.table("discount_codes").update({
        "times_used": dc["times_used"] + 1,
    }).eq("id", dc["id"]).execute()
    return True, f"Rabattkod {code.upper()} tillampad! ({dc['discount_percent']}% rabatt)"


# ── Daily Message Counts ─────────────────────────────────────────

def get_daily_message_count(user_id: str, access_token: str) -> int:
    client = get_client()
    client.postgrest.auth(access_token)
    today = date.today().isoformat()
    result = (
        client.table("daily_message_counts")
        .select("count")
        .eq("user_id", user_id)
        .eq("message_date", today)
        .execute()
    )
    return result.data[0]["count"] if result.data else 0


def increment_daily_messages(user_id: str, access_token: str) -> int:
    client = get_client()
    client.postgrest.auth(access_token)
    today = date.today().isoformat()
    result = (
        client.table("daily_message_counts")
        .select("id, count")
        .eq("user_id", user_id)
        .eq("message_date", today)
        .execute()
    )
    if result.data:
        new_count = result.data[0]["count"] + 1
        client.table("daily_message_counts").update(
            {"count": new_count}
        ).eq("id", result.data[0]["id"]).execute()
        return new_count
    client.table("daily_message_counts").insert({
        "user_id": user_id, "message_date": today, "count": 1,
    }).execute()
    return 1


# ── Intervals.icu Settings ───────────────────────────────────────

def get_intervals_settings(user_id: str, access_token: str) -> dict | None:
    profile = get_profile(user_id, access_token)
    if not profile:
        return None
    api_key = profile.get("intervals_api_key")
    athlete_id = profile.get("intervals_athlete_id")
    if api_key and athlete_id:
        return {"api_key": api_key, "athlete_id": athlete_id}
    return None


def save_intervals_settings(user_id: str, access_token: str, api_key: str, athlete_id: str):
    update_profile(user_id, access_token, {
        "intervals_api_key": api_key,
        "intervals_athlete_id": athlete_id,
    })
