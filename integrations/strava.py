"""
integrations/strava.py — Strava OAuth + Activity API client.
Stateless functions, no framework dependency.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

import requests

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"

SPORT_MAP = {
    "Run": "Lopning",
    "TrailRun": "Lopning",
    "Ride": "Cykel",
    "VirtualRide": "Cykel",
    "EBikeRide": "Cykel",
    "MountainBikeRide": "Cykel",
    "GravelRide": "Cykel",
    "Swim": "Sim",
    "OpenWaterSwim": "Sim",
    "Walk": "Promenad",
    "Hike": "Vandring",
    "WeightTraining": "Styrka",
    "Workout": "Styrka",
    "Yoga": "Yoga",
}


# ── OAuth ────────────────────────────────────────────────────────

def get_authorization_url(redirect_uri: str, state: str,
                          client_id: str | None = None) -> str:
    """Build Strava OAuth authorization URL.

    Uses user-provided client_id if available, falls back to env var.
    """
    cid = client_id or os.environ.get("STRAVA_CLIENT_ID", "")
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": "activity:read_all",
        "state": state,
    }
    return f"{STRAVA_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str,
                  client_id: str | None = None,
                  client_secret: str | None = None) -> dict:
    """Exchange authorization code for tokens.

    Uses user-provided credentials if available, falls back to env vars.
    """
    cid = client_id or os.environ.get("STRAVA_CLIENT_ID", "")
    secret = client_secret or os.environ.get("STRAVA_CLIENT_SECRET", "")
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": cid,
        "client_secret": secret,
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(refresh_token: str,
                         client_id: str | None = None,
                         client_secret: str | None = None) -> dict:
    """Refresh an expired access token.

    Uses user-provided credentials if available, falls back to env vars.
    """
    cid = client_id or os.environ.get("STRAVA_CLIENT_ID", "")
    secret = client_secret or os.environ.get("STRAVA_CLIENT_SECRET", "")
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": cid,
        "client_secret": secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def ensure_fresh_token(tokens: dict,
                       client_id: str | None = None,
                       client_secret: str | None = None) -> dict:
    """Check if tokens are expired, refresh if needed.

    Uses user-provided credentials for refresh.
    """
    if tokens.get("expires_at", 0) < time.time() + 60:
        refreshed = refresh_access_token(
            tokens["refresh_token"],
            client_id=client_id,
            client_secret=client_secret,
        )
        tokens["access_token"] = refreshed["access_token"]
        tokens["refresh_token"] = refreshed["refresh_token"]
        tokens["expires_at"] = refreshed["expires_at"]
        tokens["_refreshed"] = True
    return tokens


# ── OAuth State (HMAC signing) ───────────────────────────────────

def sign_state(user_id: str) -> str:
    """Create HMAC-signed state parameter encoding user_id."""
    secret = os.environ.get("STRAVA_STATE_SECRET", "trixa-default-secret")
    sig = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{user_id}:{sig}"


def verify_state(state: str) -> str | None:
    """Verify HMAC-signed state, return user_id or None."""
    if ":" not in state:
        return None
    user_id, sig = state.rsplit(":", 1)
    secret = os.environ.get("STRAVA_STATE_SECRET", "trixa-default-secret")
    expected = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:16]
    if hmac.compare_digest(sig, expected):
        return user_id
    return None


# ── Activities API ───────────────────────────────────────────────

def get_activities(access_token: str, after: int | None = None,
                    per_page: int = 200, max_pages: int = 5) -> list[dict]:
    """Fetch activities from Strava API with pagination.

    Strava returns max 200 per page. We paginate up to max_pages
    to get a full history (200 * 5 = 1000 activities max).
    """
    all_activities = []
    for page in range(1, max_pages + 1):
        params = {"per_page": per_page, "page": page}
        if after:
            params["after"] = after
        resp = requests.get(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=30,
        )
        if resp.status_code == 429:
            break  # Rate limited
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break  # No more activities
        all_activities.extend(batch)
        if len(batch) < per_page:
            break  # Last page
    return all_activities


def parse_activity(raw: dict) -> dict:
    """Map a Strava activity to our schema."""
    sport = raw.get("type", raw.get("sport_type", "Unknown"))
    distance_m = raw.get("distance", 0)
    distance_km = round(distance_m / 1000, 2) if distance_m else None
    moving_time = raw.get("moving_time", 0)
    duration_min = round(moving_time / 60, 1) if moving_time else None

    # Calculate pace for running/swimming
    pace = None
    if sport in ("Run", "TrailRun") and distance_m and moving_time:
        pace_sec_per_km = moving_time / (distance_m / 1000)
        mins = int(pace_sec_per_km // 60)
        secs = int(pace_sec_per_km % 60)
        pace = f"{mins}:{secs:02d}/km"
    elif sport in ("Swim", "OpenWaterSwim") and distance_m and moving_time:
        pace_sec_per_100 = moving_time / (distance_m / 100)
        mins = int(pace_sec_per_100 // 60)
        secs = int(pace_sec_per_100 % 60)
        pace = f"{mins}:{secs:02d}/100m"

    # Ensure integer fields are int (Strava sometimes returns floats)
    avg_hr = raw.get("average_heartrate")
    avg_power = raw.get("average_watts")
    elevation = raw.get("total_elevation_gain")

    return {
        "strava_id": int(raw["id"]),
        "date": raw.get("start_date_local", "")[:10],
        "type": SPORT_MAP.get(sport, sport),
        "name": raw.get("name", ""),
        "duration_min": duration_min,
        "distance_km": distance_km,
        "avg_hr": int(avg_hr) if avg_hr else None,
        "avg_power": int(avg_power) if avg_power else None,
        "elevation_m": round(elevation, 1) if elevation else None,
        "pace": pace,
    }
