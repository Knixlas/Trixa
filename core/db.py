"""
core/db.py — Supabase operations for Trixa.
All queries are user_id-scoped.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from httpx import Timeout
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions

TRIAL_DAYS = 30

# Supabase free tier can be slow — increase timeouts
_SB_TIMEOUT = Timeout(15.0, connect=10.0)
_SB_OPTIONS = ClientOptions(postgrest_client_timeout=15)


def get_client() -> Client:
    return create_client(
        os.environ.get("SUPABASE_URL", ""),
        os.environ.get("SUPABASE_ANON_KEY", ""),
        options=_SB_OPTIONS,
    )


def get_admin_client() -> Client:
    return create_client(
        os.environ.get("SUPABASE_URL", ""),
        os.environ.get("SUPABASE_SERVICE_KEY", ""),
        options=_SB_OPTIONS,
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


# ── Strava ───────────────────────────────────────────────────────

def save_strava_tokens(user_id: str, tokens: dict):
    """Upsert Strava tokens (uses admin client for insert)."""
    admin = get_admin_client()
    existing = admin.table("strava_tokens").select("id").eq("user_id", user_id).execute()
    data = {
        "user_id": user_id,
        "athlete_id": tokens.get("athlete", {}).get("id", tokens.get("athlete_id", 0)),
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": tokens["expires_at"],
    }
    if existing.data:
        admin.table("strava_tokens").update(data).eq("user_id", user_id).execute()
    else:
        admin.table("strava_tokens").insert(data).execute()


def get_strava_tokens(user_id: str, access_token: str) -> dict | None:
    client = get_client()
    client.postgrest.auth(access_token)
    result = client.table("strava_tokens").select("*").eq("user_id", user_id).execute()
    return result.data[0] if result.data else None


def update_strava_tokens(user_id: str, new_tokens: dict):
    """Update tokens after refresh (admin client)."""
    admin = get_admin_client()
    admin.table("strava_tokens").update({
        "access_token": new_tokens["access_token"],
        "refresh_token": new_tokens["refresh_token"],
        "expires_at": new_tokens["expires_at"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("user_id", user_id).execute()


def delete_strava_tokens(user_id: str):
    admin = get_admin_client()
    admin.table("strava_tokens").delete().eq("user_id", user_id).execute()


def upsert_strava_activities(user_id: str, activities: list[dict]) -> int:
    """Batch upsert activities. Returns count."""
    admin = get_admin_client()
    count = 0
    for act in activities:
        act["user_id"] = user_id
        existing = admin.table("strava_activities").select("id").eq("strava_id", act["strava_id"]).execute()
        if existing.data:
            admin.table("strava_activities").update(act).eq("strava_id", act["strava_id"]).execute()
        else:
            admin.table("strava_activities").insert(act).execute()
        count += 1
    return count


def get_recent_strava_activities(user_id: str, access_token: str, days: int = 14) -> list[dict]:
    client = get_client()
    client.postgrest.auth(access_token)
    since = (date.today() - timedelta(days=days)).isoformat()
    result = (
        client.table("strava_activities")
        .select("*")
        .eq("user_id", user_id)
        .gte("date", since)
        .order("date", desc=True)
        .execute()
    )
    return result.data or []


# ── Coach Memory ────────────────────────────────────────────────

def get_coach_memories(user_id: str, access_token: str) -> list[dict]:
    """Get coach memory observations for a user, ordered by confidence."""
    client = get_client()
    client.postgrest.auth(access_token)
    try:
        result = (
            client.table("coach_memory")
            .select("category, observation, confidence, times_seen")
            .eq("user_id", user_id)
            .order("confidence", desc=True)
            .limit(10)
            .execute()
        )
        return result.data or []
    except Exception:
        return []


# ── Coach Brief (dashboard cache) ───────────────────────────────

def get_coach_brief(user_id: str, access_token: str) -> dict | None:
    """Get today's cached coach brief."""
    client = get_client()
    client.postgrest.auth(access_token)
    today = date.today().isoformat()
    try:
        result = (
            client.table("coach_briefs")
            .select("*")
            .eq("user_id", user_id)
            .eq("brief_date", today)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception:
        return None


def save_coach_brief(user_id: str, brief: str, follow_up: str | None = None):
    """Save or update today's coach brief."""
    admin = get_admin_client()
    today = date.today().isoformat()
    existing = (
        admin.table("coach_briefs")
        .select("id")
        .eq("user_id", user_id)
        .eq("brief_date", today)
        .execute()
    )
    data = {
        "user_id": user_id,
        "brief_date": today,
        "brief": brief,
        "follow_up": follow_up,
    }
    if existing.data:
        admin.table("coach_briefs").update(data).eq("id", existing.data[0]["id"]).execute()
    else:
        admin.table("coach_briefs").insert(data).execute()


# ── Planned Sessions ────────────────────────────────────────────

def get_planned_sessions(user_id: str, access_token: str, from_date: str, to_date: str) -> list[dict]:
    client = get_client()
    client.postgrest.auth(access_token)
    result = (
        client.table("planned_sessions")
        .select("*")
        .eq("user_id", user_id)
        .gte("date", from_date)
        .lte("date", to_date)
        .order("date")
        .execute()
    )
    return result.data or []


def upsert_planned_session(user_id: str, session: dict):
    """Upsert a single planned session by user_id + date + sport."""
    admin = get_admin_client()
    session["user_id"] = user_id
    session["updated_at"] = datetime.now(timezone.utc).isoformat()
    existing = (
        admin.table("planned_sessions")
        .select("id")
        .eq("user_id", user_id)
        .eq("date", session["date"])
        .eq("sport", session.get("sport", ""))
        .execute()
    )
    if existing.data:
        admin.table("planned_sessions").update(session).eq("id", existing.data[0]["id"]).execute()
    else:
        admin.table("planned_sessions").insert(session).execute()


def upsert_planned_sessions_batch(user_id: str, sessions: list[dict]):
    """Upsert multiple planned sessions."""
    for s in sessions:
        upsert_planned_session(user_id, s)
