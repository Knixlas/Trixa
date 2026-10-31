"""
core/db.py — Supabase operations for Trixa.
All queries are user_id-scoped.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import threading
import time
from collections import OrderedDict

from supabase import create_client, Client

TRIAL_DAYS = 30

_TIMEOUT = 30.0  # Supabase free tier can cold-start for 20-30s

# Singletons — one connection pool per process, not one per request
_client: Client | None = None
_admin_client: Client | None = None


def _create_client(url: str, key: str) -> Client:
    client = create_client(url, key)
    for attr in ("postgrest.session", "auth._http_client"):
        try:
            obj = client
            for part in attr.split("."):
                obj = getattr(obj, part)
            obj.timeout = _TIMEOUT
        except Exception:
            pass
    return client


def get_client() -> Client:
    global _client
    if _client is None:
        _client = _create_client(
            os.environ.get("SUPABASE_URL", ""),
            os.environ.get("SUPABASE_ANON_KEY", ""),
        )
    return _client


_user_clients: "OrderedDict[str, Client]" = OrderedDict()
_user_clients_lock = threading.Lock()
_MAX_USER_CLIENTS = 64


def get_user_client(access_token: str) -> Client:
    """Client bound to one user's JWT. Never mutate the shared anon client's
    auth header per request: concurrent requests (threadpool, SSE generators)
    would otherwise run queries with another user's token."""
    with _user_clients_lock:
        c = _user_clients.get(access_token)
        if c is not None:
            _user_clients.move_to_end(access_token)
            return c
    c = _create_client(
        os.environ.get("SUPABASE_URL", ""),
        os.environ.get("SUPABASE_ANON_KEY", ""),
    )
    c.postgrest.auth(access_token)
    with _user_clients_lock:
        _user_clients[access_token] = c
        while len(_user_clients) > _MAX_USER_CLIENTS:
            _user_clients.popitem(last=False)
    return c


def get_admin_client() -> Client:
    global _admin_client
    if _admin_client is None:
        _admin_client = _create_client(
            os.environ.get("SUPABASE_URL", ""),
            os.environ.get("SUPABASE_SERVICE_KEY", ""),
        )
    return _admin_client


# ── Auth ─────────────────────────────────────────────────────────

def sign_up(email: str, password: str, name: str = ""):
    client = get_client()
    return client.auth.sign_up({
        "email": email,
        "password": password,
        "options": {"data": {"name": name}},
    })


def sign_in(email: str, password: str, _retries: int = 2):
    """Sign in with retry — Supabase free tier cold-starts cause timeouts."""
    last_err = None
    for attempt in range(_retries + 1):
        try:
            client = get_client()
            return client.auth.sign_in_with_password({
                "email": email,
                "password": password,
            })
        except Exception as e:
            last_err = e
            if "timed out" in str(e).lower() and attempt < _retries:
                time.sleep(2)
                continue
            raise
    raise last_err


# ── Profiles ─────────────────────────────────────────────────────

def get_profile(user_id: str, access_token: str) -> dict | None:
    client = get_user_client(access_token)
    result = client.table("profiles").select("*").eq("id", user_id).execute()
    return result.data[0] if result.data else None


def update_profile(user_id: str, access_token: str, data: dict) -> dict:
    client = get_user_client(access_token)
    result = client.table("profiles").update(data).eq("id", user_id).execute()
    return result.data[0] if result.data else {}


def profile_to_dict(profile: dict) -> dict:
    """Convert Supabase profile row to a flat dict for system prompt.
    Includes all fields — old and new — to maximize context for Trixa."""
    d = {}
    # Identity
    for k in ("name", "display_name", "email"):
        if profile.get(k): d[k] = profile[k]
    # Experience
    for k in ("experience_level", "ironman_finishes", "years_training"):
        if profile.get(k): d[k] = profile[k]
    # Goals (new)
    for k in ("goal", "vision", "season_goal", "short_term_goal"):
        if profile.get(k): d[k] = profile[k]
    # Physical
    if profile.get("weight_kg"): d["weight_kg"] = float(profile["weight_kg"])
    if profile.get("age"): d["age"] = profile["age"]
    # Zones — old field names
    for k in ("ftp_watts", "at_pace", "lt_pace", "at_hr", "lt_hr", "css"):
        if profile.get(k): d[k] = profile[k]
    # Zones — new field names
    for k in ("ftp", "css_per_100m", "threshold_pace", "threshold_hr", "max_hr"):
        if profile.get(k): d[k] = profile[k]
    # Race
    for k in ("next_race_name", "next_race_date"):
        if profile.get(k): d[k] = profile[k]
    # Notes
    for k in ("health_notes", "preferences", "notes"):
        if profile.get(k): d[k] = profile[k]
    # Strength
    if profile.get("strength_program"): d["strength_program"] = profile["strength_program"]
    # Health & body
    for k in ("gender", "height_cm", "resting_hr", "blood_pressure",
              "medications", "injuries"):
        if profile.get(k): d[k] = profile[k]
    if profile.get("self_assessment"): d["self_assessment"] = profile["self_assessment"]
    if profile.get("health_data"): d["health_data"] = profile["health_data"]
    return d


def merge_health_data(user_id: str, access_token: str, new_data: dict) -> dict:
    """Additively merge new keys into health_data JSONB column."""
    import json as _json
    profile = get_profile(user_id, access_token)
    existing = profile.get("health_data") or {}
    if isinstance(existing, str):
        existing = _json.loads(existing)
    existing.update(new_data)
    update_profile(user_id, access_token, {"health_data": existing})
    return existing


# ── Conversations ────────────────────────────────────────────────

def get_conversation(user_id: str, access_token: str) -> dict | None:
    client = get_user_client(access_token)
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
    client = get_user_client(access_token)
    if conv_id:
        client.table("conversations").update({"messages": messages}).eq("id", conv_id).execute()
        return conv_id
    result = client.table("conversations").insert({
        "user_id": user_id,
        "messages": messages,
    }).execute()
    return result.data[0]["id"] if result.data else ""


def delete_conversation(user_id: str, access_token: str, conv_id: str):
    client = get_user_client(access_token)
    client.table("conversations").delete().eq("id", conv_id).eq("user_id", user_id).execute()


# ── Subscriptions ────────────────────────────────────────────────

def get_subscription(user_id: str, access_token: str) -> dict | None:
    client = get_user_client(access_token)
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
    client = get_user_client(access_token)
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
    client = get_user_client(access_token)
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
    client = get_user_client(access_token)
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
    """Batch upsert activities into both strava_activities and training_log."""
    admin = get_admin_client()
    count = 0
    for act in activities:
        act["user_id"] = user_id
        strava_id = act.get("strava_id")

        # Upsert into strava_activities
        existing = admin.table("strava_activities").select("id").eq("strava_id", strava_id).execute()
        if existing.data:
            admin.table("strava_activities").update(act).eq("strava_id", strava_id).execute()
        else:
            admin.table("strava_activities").insert(act).execute()

        # Mirror into training_log (single source of truth)
        try:
            log_entry = {
                "user_id": user_id,
                "date": act.get("date"),
                "sport": act.get("type", "other"),
                "title": act.get("name"),
                "duration_min": act.get("duration_min"),
                "distance_km": act.get("distance_km"),
                "avg_hr": act.get("avg_hr"),
                "avg_power": act.get("avg_power"),
                "pace": act.get("pace"),
                "source": "strava",
                "strava_id": strava_id,
            }
            # Remove None values
            log_entry = {k: v for k, v in log_entry.items() if v is not None}

            existing_log = admin.table("training_log").select("id").eq("strava_id", strava_id).execute()
            if existing_log.data:
                admin.table("training_log").update(log_entry).eq("strava_id", strava_id).execute()
            else:
                admin.table("training_log").insert(log_entry).execute()
        except Exception as e:
            print(f"[training_log] Mirror error for strava_id {strava_id}: {e}")

        count += 1
    return count


def get_recent_strava_activities(user_id: str, access_token: str, days: int = 14) -> list[dict]:
    client = get_user_client(access_token)
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


# ── Training Log ────────────────────────────────────────────────

def get_training_log(user_id: str, access_token: str, days: int = 60) -> list[dict]:
    """Get training log entries (from all sources) for the last N days."""
    client = get_user_client(access_token)
    since = (date.today() - timedelta(days=days)).isoformat()
    result = (
        client.table("training_log")
        .select("*")
        .eq("user_id", user_id)
        .gte("date", since)
        .order("date", desc=True)
        .execute()
    )
    return result.data or []


def get_recent_activities(user_id: str, access_token: str, days: int = 14) -> list[dict]:
    """Recent activities from training_log (all sources: strava, tp, manual),
    mapped to the strava_activities shape (type/name) the prompt builders use.
    Ratings stored on strava_activities are merged in by strava_id."""
    log = get_training_log(user_id, access_token, days=days)
    ratings: dict = {}
    try:
        for s in get_recent_strava_activities(user_id, access_token, days=days):
            if s.get("rating"):
                ratings[s.get("strava_id")] = s
    except Exception:
        pass
    out = []
    for t in log:
        a = dict(t)
        a["type"] = t.get("sport") or "other"
        a["name"] = t.get("title") or ""
        r = ratings.get(t.get("strava_id"))
        if r and not a.get("rating"):
            a["rating"] = r.get("rating")
            a["rating_comment"] = r.get("rating_comment")
        out.append(a)
    return dedupe_activities(out)


_SPORT_ALIASES = {
    "swim": "swim", "sim": "swim", "simning": "swim",
    "bike": "bike", "cykel": "bike", "ride": "bike", "virtualride": "bike",
    "run": "run", "lopning": "run", "löpning": "run", "virtualrun": "run",
    "strength": "strength", "styrka": "strength", "weighttraining": "strength",
    "yoga": "yoga", "promenad": "walk", "walk": "walk",
    "övrigt": "other", "ovrigt": "other", "vila": "rest", "rest": "rest",
}
_SYNCED_SOURCES = {"strava", "tp", "garmin"}


def _norm_sport(s: str | None) -> str:
    s = (s or "other").strip().lower()
    return _SPORT_ALIASES.get(s, s)


def _same_session(a: dict, b: dict) -> bool:
    """Same workout if durations are close, or either side lacks a duration."""
    da, db_ = a.get("duration_min") or 0, b.get("duration_min") or 0
    if not da or not db_:
        return True
    return abs(da - db_) <= 0.4 * max(da, db_)


def match_activities_to_sessions(plans: list[dict], acts: list[dict]) -> tuple[list[dict | None], list[dict]]:
    """Pair each planned session (same day) with at most one activity.
    1) explicit training_log.planned_session_id link, 2) same normalized sport.
    An activity is never used twice, and never matched across sports.
    Returns (match per plan, unmatched activities)."""
    matches: list[dict | None] = [None] * len(plans)
    used: set[int] = set()
    for i, p in enumerate(plans):
        if not p.get("id"):
            continue
        for a in acts:
            if id(a) not in used and a.get("planned_session_id") == p["id"]:
                matches[i] = a
                used.add(id(a))
                break
    for i, p in enumerate(plans):
        if matches[i] is not None:
            continue
        ps = _norm_sport(p.get("sport"))
        for a in acts:
            if id(a) not in used and _norm_sport(a.get("sport") or a.get("type")) == ps:
                matches[i] = a
                used.add(id(a))
                break
    return matches, [a for a in acts if id(a) not in used]


def dedupe_activities(acts: list[dict]) -> list[dict]:
    """Collapse the same workout logged from several sources (e.g. manual +
    TrainingPeaks, or Strava + TP). Synced entries win; notes/title/RPE from
    a dropped manual entry are carried over. Input order is preserved."""
    synced = [a for a in acts if (a.get("source") or "") in _SYNCED_SOURCES]
    keep: list[dict] = []
    dropped = set()
    # 1) Synced vs synced from different sources (strava + tp of same workout)
    for i, a in enumerate(synced):
        if id(a) in dropped:
            continue
        for b in synced[i + 1:]:
            if (id(b) not in dropped and b.get("source") != a.get("source")
                    and b.get("date") == a.get("date")
                    and _norm_sport(b.get("sport")) == _norm_sport(a.get("sport"))
                    and _same_session(a, b)):
                loser = b if sum(v is not None for v in a.values()) >= sum(v is not None for v in b.values()) else a
                dropped.add(id(loser))
                winner = a if loser is b else b
                if loser.get("rating") and not winner.get("rating"):
                    winner["rating"] = loser["rating"]
                    winner["rating_comment"] = loser.get("rating_comment")
                if loser is a:
                    break
    # 2) Manual/chat/screenshot entries matching a synced one
    for a in acts:
        if (a.get("source") or "") in _SYNCED_SOURCES or id(a) in dropped:
            continue
        match = next((s for s in synced if id(s) not in dropped
                      and s.get("date") == a.get("date")
                      and _norm_sport(s.get("sport")) == _norm_sport(a.get("sport"))
                      and _same_session(a, s)), None)
        if match:
            dropped.add(id(a))
            note_bits = [x for x in (a.get("title"), a.get("notes"), a.get("athlete_description")) if x]
            if note_bits:
                match["athlete_note"] = " — ".join(note_bits)
            for k in ("rpe", "feeling", "rating"):
                if a.get(k) and not match.get(k):
                    match[k] = a[k]
    for a in acts:
        if id(a) not in dropped:
            keep.append(a)
    return keep


# ── Garmin daily metrics (HRV, sömn, vilopuls, belastning) ──────

def get_daily_metrics(user_id: str, days: int = 14) -> list[dict]:
    """Daily health metrics synced from Garmin into garmin_coach.daily_metrics.
    Keyed by garmin_coach.athlete_profile.id, so resolve user_id first.
    Admin client, explicitly filtered by user_id. Newest first."""
    admin = get_admin_client()
    gc = admin.schema("garmin_coach")
    ap = gc.table("athlete_profile").select("id").eq("user_id", user_id).limit(1).execute()
    if not ap.data:
        return []
    since = (date.today() - timedelta(days=days)).isoformat()
    result = (
        gc.table("daily_metrics")
        .select("metric_date, hrv_status, hrv_last_night_ms, hrv_weekly_avg_ms, "
                "hrv_baseline_low, hrv_baseline_high, sleep_score, sleep_duration_sec, "
                "readiness_score, body_battery_high, body_battery_low, stress_avg, "
                "resting_hr, weight_kg, vo2max_running, vo2max_cycling, training_status, "
                "acute_load, chronic_load, load_ratio, recovery_time_hours")
        .eq("athlete_id", ap.data[0]["id"])
        .gte("metric_date", since)
        .order("metric_date", desc=True)
        .execute()
    )
    return result.data or []


def format_daily_metrics(metrics: list[dict], max_days: int = 7) -> str:
    """Compact Swedish text lines for prompts."""
    lines = []
    for m in metrics[:max_days]:
        p = [m["metric_date"]]
        if m.get("hrv_last_night_ms") is not None:
            s = f"HRV {m['hrv_last_night_ms']:g} ms"
            if m.get("hrv_weekly_avg_ms") is not None:
                s += f" (7d snitt {m['hrv_weekly_avg_ms']:g}"
                if m.get("hrv_baseline_low") is not None and m.get("hrv_baseline_high") is not None:
                    s += f", baslinje {m['hrv_baseline_low']:g}–{m['hrv_baseline_high']:g}"
                s += ")"
            p.append(s)
        if m.get("hrv_status"): p.append(f"HRV-status {m['hrv_status']}")
        if m.get("resting_hr") is not None: p.append(f"vilopuls {m['resting_hr']}")
        if m.get("sleep_score") is not None: p.append(f"sömnpoäng {m['sleep_score']}")
        if m.get("sleep_duration_sec"): p.append(f"sömn {m['sleep_duration_sec'] / 3600:.1f} h")
        if m.get("readiness_score") is not None: p.append(f"readiness {m['readiness_score']}")
        if m.get("body_battery_high") is not None: p.append(f"body battery {m.get('body_battery_low')}–{m['body_battery_high']}")
        if m.get("stress_avg") is not None: p.append(f"stress {m['stress_avg']}")
        if m.get("acute_load") is not None and m.get("chronic_load") is not None:
            s = f"belastning akut {m['acute_load']:g}/kronisk {m['chronic_load']:g}"
            if m.get("load_ratio") is not None:
                s += f" (kvot {m['load_ratio']:.2f})"
            p.append(s)
        if m.get("training_status"): p.append(f"status {m['training_status']}")
        if m.get("recovery_time_hours"): p.append(f"återhämtning {m['recovery_time_hours']} h")
        if m.get("weight_kg") is not None: p.append(f"vikt {m['weight_kg']:g} kg")
        if m.get("vo2max_running") is not None: p.append(f"VO2max löp {m['vo2max_running']:g}")
        if m.get("vo2max_cycling") is not None: p.append(f"VO2max cykel {m['vo2max_cycling']:g}")
        if len(p) > 1:
            lines.append("- " + ", ".join(p))
    return "\n".join(lines)


# ── Coach Memory ────────────────────────────────────────────────

def save_memory_observations(user_id: str, observations: list[dict]):
    """Save coach memory observations. If similar exists, increment confidence."""
    admin = get_admin_client()
    for obs in observations:
        cat = obs.get("category", "behavior")
        text = obs.get("observation", "")
        if not text:
            continue
        # Check for similar existing observation
        try:
            existing = (
                admin.table("coach_memory")
                .select("id, times_seen, confidence")
                .eq("user_id", user_id)
                .eq("category", cat)
                .ilike("observation", f"%{text[:30]}%")
                .execute()
            )
            if existing.data:
                row = existing.data[0]
                new_conf = min(1.0, row["confidence"] + 0.1)
                admin.table("coach_memory").update({
                    "times_seen": row["times_seen"] + 1,
                    "confidence": new_conf,
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                }).eq("id", row["id"]).execute()
            else:
                admin.table("coach_memory").insert({
                    "user_id": user_id,
                    "category": cat,
                    "observation": text,
                    "confidence": 0.6,
                }).execute()
        except Exception as e:
            print(f"[memory] observation save error: {e}")


def get_coach_memories(user_id: str, access_token: str) -> list[dict]:
    """Get coach memory observations for a user, ordered by confidence."""
    client = get_user_client(access_token)
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
    client = get_user_client(access_token)
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
    client = get_user_client(access_token)
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
    """Replace planned sessions for the date range covered by the new batch.

    Deletes all existing sessions within the date range first, then inserts
    the new ones. This ensures removed or changed sessions don't leave stale data.
    """
    if not sessions:
        return
    admin = get_admin_client()
    dates = [s["date"] for s in sessions if "date" in s]
    if dates:
        min_date = min(dates)
        max_date = max(dates)
        admin.table("planned_sessions") \
            .delete() \
            .eq("user_id", user_id) \
            .gte("date", min_date) \
            .lte("date", max_date) \
            .execute()
    for s in sessions:
        s["user_id"] = user_id
        s["updated_at"] = datetime.now(timezone.utc).isoformat()
        admin.table("planned_sessions").insert(s).execute()
