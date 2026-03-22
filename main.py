"""
main.py — Trixa FastAPI backend.
Serves API endpoints + static frontend.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import anthropic

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env", override=True)

from core import db
from core.membership import (
    get_user_tier, can_send_message, can_use_feature,
    messages_remaining, trial_days_remaining,
)

SYSTEM_PROMPT_FILE = ROOT / "prompts" / "system_prompt.md"
MODEL = "claude-sonnet-4-5"
MAX_HISTORY = 20
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")

app = FastAPI(title="Trixa API")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


# ── Request schemas ──────────────────────────────────────────────

class AuthRequest(BaseModel):
    email: str
    password: str
    name: str | None = None

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []

class IntervalsPushRequest(BaseModel):
    workout: dict

class DiscountRequest(BaseModel):
    code: str


# ── Helpers ──────────────────────────────────────────────────────

WEEKDAYS_SV = ["mandag", "tisdag", "onsdag", "torsdag", "fredag", "lordag", "sondag"]

WORKOUT_TOOL = {
    "name": "create_workout_file",
    "description": (
        "Skapa ett strukturerat traningspass som pushas till Intervals.icu. "
        "For lopning: ange ALLTID hr_high. For cykling: ange ALLTID power_high. "
        "Ange description pa varje steg — det visas pa klockan."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "sport": {"type": "string", "enum": ["running", "biking", "swimming"]},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["warmup", "active", "rest", "cooldown"]},
                        "duration_seconds": {"type": "integer"},
                        "repeats": {"type": "integer"},
                        "description": {"type": "string"},
                        "hr_high": {"type": "integer"},
                        "power_high": {"type": "integer"},
                    },
                    "required": ["type", "duration_seconds", "description"],
                },
            },
        },
        "required": ["name", "sport", "steps"],
    },
}


def _build_system_prompt(profile: dict | None, activities: list[dict] | None = None) -> str:
    template = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    now = datetime.now()
    template = (
        template
        .replace("{TODAY_DATE}", now.strftime("%Y-%m-%d"))
        .replace("{TODAY_WEEKDAY}", WEEKDAYS_SV[now.weekday()])
    )
    if profile:
        p = db.profile_to_dict(profile)
        lines = [f"- {k}: {v}" for k, v in p.items() if v]
        template = template.replace("{ATHLETE_PROFILE}", "\n".join(lines))
    else:
        template = template.replace("{ATHLETE_PROFILE}", "Ingen profil tillganglig.")

    # Inject recent Strava activities
    if activities:
        lines = []
        for a in activities[:20]:
            parts = [f"- {a['date']}: {a['type']} \"{a.get('name', '')}\""]
            if a.get("duration_min"):
                parts.append(f"{a['duration_min']} min")
            if a.get("distance_km"):
                parts.append(f"{a['distance_km']} km")
            if a.get("pace"):
                parts.append(a["pace"])
            if a.get("avg_hr"):
                parts.append(f"puls {a['avg_hr']}")
            if a.get("avg_power"):
                parts.append(f"{a['avg_power']}W")
            lines.append(", ".join(parts))
        template = template.replace("{RECENT_ACTIVITIES}", "\n".join(lines))
    else:
        template = template.replace("{RECENT_ACTIVITIES}", "Ingen Strava-koppling eller inga aktiviteter.")
    return template


def _get_auth(request: Request) -> tuple[str, str]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing authorization")
    token = auth[7:]
    uid = request.headers.get("X-User-Id", "")
    if not uid:
        raise HTTPException(401, "Missing user ID")
    return uid, token


# ── Frontend ─────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    return HTMLResponse((ROOT / "static" / "index.html").read_text(encoding="utf-8"))


# ── Auth ─────────────────────────────────────────────────────────

@app.post("/api/auth/login")
async def login(req: AuthRequest):
    try:
        result = db.sign_in(req.email, req.password)
        if not result.user:
            raise HTTPException(401, "Invalid credentials")
        sub = None
        try:
            sub = db.ensure_trial(result.user.id, result.session.access_token)
        except Exception:
            pass
        is_admin = (result.user.email == ADMIN_EMAIL)
        tier = get_user_tier(sub, is_admin)
        return {
            "user_id": result.user.id,
            "email": result.user.email,
            "name": result.user.user_metadata.get("name", ""),
            "access_token": result.session.access_token,
            "tier": tier,
            "is_admin": is_admin,
            "trial_days": trial_days_remaining(sub),
        }
    except HTTPException:
        raise
    except Exception as e:
        if "Invalid login" in str(e):
            raise HTTPException(401, "Fel e-post eller losenord")
        raise HTTPException(400, str(e))


@app.post("/api/auth/signup")
async def signup(req: AuthRequest):
    if not req.name:
        raise HTTPException(400, "Name required")
    try:
        result = db.sign_up(req.email, req.password, req.name)
        if result.user:
            return {"ok": True, "message": "Konto skapat! Logga in."}
        raise HTTPException(400, "Signup failed")
    except Exception as e:
        raise HTTPException(400, str(e))


# ── Profile ──────────────────────────────────────────────────────

@app.get("/api/profile")
async def get_profile(request: Request):
    uid, token = _get_auth(request)
    return {"profile": db.get_profile(uid, token)}


# ── Subscription ─────────────────────────────────────────────────

@app.get("/api/subscription")
async def get_subscription(request: Request):
    uid, token = _get_auth(request)
    sub = db.get_subscription(uid, token)
    is_admin = (request.headers.get("X-User-Email", "") == ADMIN_EMAIL)
    tier = get_user_tier(sub, is_admin)
    remaining = None
    try:
        daily_count = db.get_daily_message_count(uid, token)
        remaining = messages_remaining(tier, daily_count)
    except Exception:
        pass
    return {"tier": tier, "trial_days": trial_days_remaining(sub), "messages_remaining": remaining}


# ── Chat (SSE streaming) ────────────────────────────────────────

@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    uid, token = _get_auth(request)

    # Tier + message limit check
    sub = None
    try:
        sub = db.get_subscription(uid, token)
    except Exception:
        pass
    is_admin = (request.headers.get("X-User-Email", "") == ADMIN_EMAIL)
    tier = get_user_tier(sub, is_admin)

    try:
        daily_count = db.get_daily_message_count(uid, token)
    except Exception:
        daily_count = 0
    if not can_send_message(tier, daily_count):
        raise HTTPException(429, "Daglig meddelandegrans nadd. Uppgradera till Premium!")

    try:
        db.increment_daily_messages(uid, token)
    except Exception:
        pass

    # Build system prompt from profile + activities
    profile = db.get_profile(uid, token)
    try:
        activities = db.get_recent_strava_activities(uid, token, days=14)
    except Exception:
        activities = None
    system_prompt = _build_system_prompt(profile, activities)

    # Prepare messages
    clean = [{"role": m["role"], "content": m["content"]} for m in req.history[-MAX_HISTORY:]]
    clean.append({"role": "user", "content": req.message})

    api_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    tools = [WORKOUT_TOOL] if can_use_feature(tier, "workout_export") else None

    # Non-streaming when tools enabled (to handle tool_use blocks)
    if tools:
        response_obj = api_client.messages.create(
            model=MODEL, max_tokens=2048, system=system_prompt,
            messages=clean, tools=tools,
        )
        text_parts = []
        workout_data = None
        for block in response_obj.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use" and block.name == "create_workout_file":
                workout_data = block.input

        response_text = "\n".join(text_parts)
        _save_conv(uid, token, req.history, req.message, response_text)
        result = {"text": response_text}
        if workout_data:
            result["workout"] = workout_data
        return result

    # Streaming (no tools)
    def stream_response():
        full_text = ""
        with api_client.messages.stream(
            model=MODEL, max_tokens=2048, system=system_prompt, messages=clean,
        ) as stream:
            for text in stream.text_stream:
                full_text += text
                yield f"data: {json.dumps({'text': text})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"
        _save_conv(uid, token, req.history, req.message, full_text)

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _save_conv(uid, token, history, message, response):
    try:
        full = history + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": response},
        ]
        db.save_conversation(uid, token, full, None)
    except Exception:
        pass


# ── Intervals.icu ────────────────────────────────────────────────

@app.post("/api/intervals/push")
async def push_to_intervals(req: IntervalsPushRequest, request: Request):
    uid, token = _get_auth(request)
    icu_cfg = db.get_intervals_settings(uid, token)
    if not icu_cfg:
        raise HTTPException(400, "Intervals.icu ej konfigurerat")
    from integrations.intervals_icu import push_workout
    result = push_workout(icu_cfg["api_key"], icu_cfg["athlete_id"], req.workout)
    if result.get("success"):
        return {"ok": True}
    raise HTTPException(400, result.get("error", "Unknown error"))


@app.post("/api/intervals/save")
async def save_intervals_settings(request: Request):
    uid, token = _get_auth(request)
    body = await request.json()
    db.save_intervals_settings(uid, token, body.get("api_key", ""), body.get("athlete_id", ""))
    return {"ok": True}


# ── Discount ─────────────────────────────────────────────────────

@app.post("/api/discount/apply")
async def apply_discount(req: DiscountRequest, request: Request):
    uid, _ = _get_auth(request)
    ok, msg = db.apply_discount_code(uid, req.code)
    if ok:
        return {"ok": True, "message": msg}
    raise HTTPException(400, msg)


# ── Conversation ─────────────────────────────────────────────────

@app.get("/api/conversation")
async def get_conversation(request: Request):
    uid, token = _get_auth(request)
    conv = db.get_conversation(uid, token)
    if conv:
        return {"messages": conv.get("messages", []), "id": conv.get("id")}
    return {"messages": [], "id": None}


@app.delete("/api/conversation")
async def clear_conversation(request: Request):
    uid, token = _get_auth(request)
    conv = db.get_conversation(uid, token)
    if conv:
        db.delete_conversation(uid, token, conv["id"])
    return {"ok": True}


# ── Strava ───────────────────────────────────────────────────────

@app.get("/api/strava/connect")
async def strava_connect(request: Request):
    uid, _ = _get_auth(request)
    from integrations.strava import get_authorization_url, sign_state
    redirect_uri = os.environ.get(
        "STRAVA_REDIRECT_URI",
        str(request.base_url).rstrip("/") + "/api/strava/callback",
    )
    state = sign_state(uid)
    url = get_authorization_url(redirect_uri, state)
    return {"url": url}


@app.get("/api/strava/callback")
async def strava_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """OAuth callback — browser redirect from Strava."""
    if error:
        return RedirectResponse("/?strava=error")

    from integrations.strava import verify_state, exchange_code, get_activities, parse_activity
    user_id = verify_state(state)
    if not user_id:
        return RedirectResponse("/?strava=error")

    redirect_uri = os.environ.get(
        "STRAVA_REDIRECT_URI",
        str(request.base_url).rstrip("/") + "/api/strava/callback",
    )

    try:
        tokens = exchange_code(code, redirect_uri)
        db.save_strava_tokens(user_id, tokens)

        # Initial sync (last 30 days)
        import time
        after = int(time.time()) - 30 * 86400
        raw_activities = get_activities(tokens["access_token"], after=after)
        parsed = [parse_activity(a) for a in raw_activities]
        db.upsert_strava_activities(user_id, parsed)
    except Exception as e:
        print(f"Strava callback error: {e}")
        return RedirectResponse("/?strava=error")

    return RedirectResponse("/?strava=connected")


@app.post("/api/strava/sync")
async def strava_sync(request: Request):
    uid, token = _get_auth(request)
    strava_tokens = db.get_strava_tokens(uid, token)
    if not strava_tokens:
        raise HTTPException(400, "Strava ej kopplat")

    from integrations.strava import ensure_fresh_token, get_activities, parse_activity

    # Refresh tokens if needed
    strava_tokens = ensure_fresh_token(strava_tokens)
    if strava_tokens.get("_refreshed"):
        db.update_strava_tokens(uid, strava_tokens)

    # Fetch last 30 days
    import time
    after = int(time.time()) - 30 * 86400
    raw = get_activities(strava_tokens["access_token"], after=after)
    parsed = [parse_activity(a) for a in raw]
    count = db.upsert_strava_activities(uid, parsed)
    return {"synced": count}


@app.get("/api/strava/activities")
async def strava_activities(request: Request, days: int = 14):
    uid, token = _get_auth(request)
    strava_tokens = db.get_strava_tokens(uid, token)
    activities = db.get_recent_strava_activities(uid, token, days=days)
    return {"activities": activities, "connected": strava_tokens is not None}


@app.delete("/api/strava/disconnect")
async def strava_disconnect(request: Request):
    uid, _ = _get_auth(request)
    db.delete_strava_tokens(uid)
    return {"ok": True}
