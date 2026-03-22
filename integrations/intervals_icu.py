"""
Intervals.icu API integration for pushing structured workouts.
Uses the bulk events endpoint with text description format.
"""
from __future__ import annotations

import base64
import requests

API_BASE = "https://intervals.icu/api/v1"

SPORT_MAP = {
    "running": "Run",
    "biking": "Ride",
    "swimming": "Swim",
    "run": "Run",
    "ride": "Ride",
    "swim": "Swim",
    "cycling": "Ride",
}


def _auth_header(api_key: str) -> dict:
    """Build Basic Auth header for Intervals.icu."""
    token = base64.b64encode(f"API_KEY:{api_key}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }


def workout_to_description(workout: dict) -> str:
    """Convert our workout tool output to Intervals.icu text description format.

    Uses section headers (Warmup, Main Set, Cooldown) so Intervals.icu
    correctly categorizes each step type. Repeat blocks use 'Main Set Nx'.

    Example output:
        Warmup
        - 15m 55%

        Main Set 5x
        - 5m 95-105%
        - 1m30s 40%

        Cooldown
        - 10m 50%
    """
    sections = []
    steps = workout.get("steps", [])

    # Group steps into sections: warmup, main (active+rest), cooldown
    warmup_steps = []
    main_steps = []
    cooldown_steps = []
    main_repeats = 1

    i = 0
    while i < len(steps):
        step = steps[i]
        step_type = step.get("type", "active")

        if step_type == "warmup":
            warmup_steps.append(step)
        elif step_type == "cooldown":
            cooldown_steps.append(step)
        elif step_type in ("active", "rest"):
            # Active step with repeats: this defines the main set repeat count
            if step_type == "active" and step.get("repeats", 1) > 1:
                main_repeats = step["repeats"]
            main_steps.append(step)
        i += 1

    # Build warmup section
    if warmup_steps:
        lines = ["Warmup"]
        for s in warmup_steps:
            lines.append(_format_step(s))
        sections.append("\n".join(lines))

    # Build main set section
    if main_steps:
        header = f"Main Set {main_repeats}x" if main_repeats > 1 else "Main Set"
        lines = [header]
        for s in main_steps:
            lines.append(_format_step(s))
        sections.append("\n".join(lines))

    # Build cooldown section
    if cooldown_steps:
        lines = ["Cooldown"]
        for s in cooldown_steps:
            lines.append(_format_step(s))
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def _format_step(step: dict) -> str:
    """Format a single step line with optional text cue and intensity.

    Intervals.icu format: '- TextCue 5m 165bpm HR'
    The text cue shows on the watch during countdown.
    """
    dur = _format_duration(step.get("duration_seconds", 0))
    intensity = _format_intensity(step)
    desc = step.get("description", "")

    parts = ["-"]
    if desc:
        parts.append(desc)
    parts.append(dur)
    if intensity:
        parts.append(intensity)
    return " ".join(parts)


def _format_duration(seconds: int) -> str:
    """Format seconds to human-readable duration."""
    if seconds <= 0:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    remaining = seconds % 60
    if remaining == 0:
        return f"{minutes}m"
    return f"{minutes}m{remaining}s"


def _format_intensity(step: dict) -> str:
    """Format intensity target for Intervals.icu description.

    Uses upper limit only (as requested by user).
    Intervals.icu supports: 165bpm HR, 280w, Z3, 75%
    """
    # HR target (upper limit) — used for running
    hr_high = step.get("hr_high")
    if hr_high:
        return f"{hr_high}bpm HR"

    # Power target (upper limit) — used for cycling
    power_high = step.get("power_high")
    if power_high:
        return f"{power_high}w"

    return ""


def push_workout(
    api_key: str,
    athlete_id: str,
    workout: dict,
    date: str | None = None,
) -> dict:
    """Push a structured workout to Intervals.icu calendar.

    Args:
        api_key: Intervals.icu API key
        athlete_id: Athlete ID (or "0" for authenticated user)
        workout: Workout dict from Claude tool_use (name, sport, steps)
        date: Optional ISO date string (YYYY-MM-DD). Defaults to today.

    Returns:
        API response dict or error dict
    """
    if not date:
        from datetime import date as d
        date = d.today().isoformat()

    sport = SPORT_MAP.get(workout.get("sport", "running").lower(), "Run")
    description = workout_to_description(workout)
    total_seconds = sum(
        s.get("duration_seconds", 0) * s.get("repeats", 1) for s in workout.get("steps", [])
    )

    event = {
        "category": "WORKOUT",
        "start_date_local": f"{date}T00:00:00",
        "name": workout.get("name", "Workout"),
        "type": sport,
        "description": description,
        "moving_time": total_seconds,
    }

    url = f"{API_BASE}/athlete/{athlete_id}/events/bulk"
    headers = _auth_header(api_key)

    try:
        resp = requests.post(url, json=[event], headers=headers, timeout=15)
        if resp.status_code in (200, 201):
            return {"success": True, "data": resp.json()}
        else:
            return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
