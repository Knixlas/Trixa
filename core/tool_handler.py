"""
core/tool_handler.py — Process Claude tool_use blocks and execute DB operations.
Keeps the chat endpoint clean by centralizing all tool execution logic.
"""
from __future__ import annotations

import json
from datetime import datetime

from core import db


# Fields allowed to be updated via update_athlete_profile
PROFILE_ALLOWED_FIELDS = {
    "experience_level", "age", "weight_kg", "years_training",
    "ironman_finishes", "weekly_hours", "next_race_name",
    "health_notes", "goal", "strength_program",
    # Health & body
    "gender", "height_cm", "resting_hr", "blood_pressure",
    "medications", "injuries", "self_assessment",
}


class ToolResult:
    """Collects results from processing a full Claude response."""

    def __init__(self):
        self.text_parts: list[str] = []
        self.tool_results: list[dict] = []
        self.workout_data: dict | None = None
        self.zones_update: dict | None = None
        self.goals_update: dict | None = None
        self.plan_saved: bool = False

    def to_response(self) -> dict:
        """Build the JSON response dict for the chat endpoint."""
        result = {"text": "\n".join(self.text_parts)}
        if self.workout_data:
            result["workout"] = self.workout_data
        if self.zones_update:
            result["zones_update"] = self.zones_update
        if self.goals_update:
            result["goals_update"] = self.goals_update
        if self.plan_saved:
            result["plan_saved"] = True
        return result


def process_tool_block(block, uid: str, token: str, result: ToolResult) -> str:
    """Process a single tool_use block. Returns the tool result content string."""

    if block.name == "create_workout_file":
        result.workout_data = block.input
        return "Workout skapad"

    if block.name == "update_athlete_zones":
        result.zones_update = block.input
        return "Nyckeltal uppdaterade"

    if block.name == "set_athlete_goals":
        result.goals_update = block.input
        try:
            fields = {k: v for k, v in block.input.items() if v}
            if fields:
                fields["goal_updated_at"] = datetime.now().isoformat()
                db.update_profile(uid, token, fields)
            return "Mal sparade"
        except Exception as e:
            print(f"Goals save error: {e}")
            return f"Fel: {e}"

    if block.name == "update_athlete_profile":
        try:
            fields = {k: v for k, v in block.input.items()
                      if k in PROFILE_ALLOWED_FIELDS and v is not None}
            # Store self_assessment timestamp
            if "self_assessment" in fields:
                fields["self_assessment_at"] = datetime.now().isoformat()
                # Also write Trixa's assessment into health_data with timestamp
                db.merge_health_data(uid, token, {
                    "trixa_assessment": {
                        "value": fields["self_assessment"],
                        "timestamp": datetime.now().isoformat(),
                    }
                })
            if fields:
                db.update_profile(uid, token, fields)
                print(f"[profile] Updated: {list(fields.keys())}")
            # Handle health_data JSONB merge separately
            hd = block.input.get("health_data")
            if hd and isinstance(hd, dict):
                db.merge_health_data(uid, token, hd)
                print(f"[profile] Merged health_data keys: {list(hd.keys())}")
            return "Profil uppdaterad"
        except Exception as e:
            print(f"Profile update error: {e}")
            return f"Fel: {e}"

    if block.name == "plan_training_sessions":
        try:
            sessions = block.input.get("sessions", [])
            db.upsert_planned_sessions_batch(uid, sessions)
            result.plan_saved = True
            print(f"[plan] Saved {len(sessions)} sessions")
            return f"Sparade {len(sessions)} pass i planen"
        except Exception as e:
            print(f"Plan save error: {e}")
            return f"Fel: {e}"

    if block.name == "log_training_session":
        try:
            entry = dict(block.input)
            entry["user_id"] = uid
            entry = {k: v for k, v in entry.items() if v is not None}
            if "extra_data" in entry and isinstance(entry["extra_data"], dict):
                entry["extra_data"] = json.dumps(entry["extra_data"])
            admin = db.get_admin_client()
            admin.table("training_log").insert(entry).execute()
            missing = []
            if not entry.get("duration_min"):
                missing.append("tid")
            if not entry.get("distance_km"):
                missing.append("distans")
            if not entry.get("avg_hr"):
                missing.append("puls")
            if not entry.get("rpe"):
                missing.append("anstrangning (RPE)")
            if missing:
                return f"Pass loggat! Saknas: {', '.join(missing)} — fraga atleten."
            return "Pass loggat med all central data!"
        except Exception as e:
            print(f"Training log error: {e}")
            return f"Fel vid loggning: {e}"

    return "OK"


def process_response(response_obj, uid: str, token: str, result: ToolResult):
    """Process all blocks in a Claude response. Populates result in-place.

    Returns a list of tool_result dicts to send back for follow-up (empty if no tool calls).
    """
    tool_results = []

    for block in response_obj.content:
        if block.type == "text":
            result.text_parts.append(block.text)
        elif block.type == "tool_use":
            content = process_tool_block(block, uid, token, result)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
            })

    result.tool_results = tool_results
    return tool_results
