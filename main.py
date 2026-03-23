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
COACHING_KB_FILE = ROOT / "prompts" / "coaching_knowledge.md"
PHASES_FILE = ROOT / "prompts" / "phases.md"
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

class ImageData(BaseModel):
    base64: str
    media_type: str = "image/jpeg"

class ChatRequest(BaseModel):
    message: str = ""
    history: list[dict] = []
    images: list[ImageData] = []

class IntervalsPushRequest(BaseModel):
    workout: dict

class DiscountRequest(BaseModel):
    code: str


# ── Helpers ──────────────────────────────────────────────────────

WEEKDAYS_SV = ["mandag", "tisdag", "onsdag", "torsdag", "fredag", "lordag", "sondag"]

SET_GOALS_TOOL = {
    "name": "set_athlete_goals",
    "description": (
        "Spara atletens mal efter att ni diskuterat och kommit overens. "
        "Anvand detta nar atleten och du har formulerat vision, sasongmal eller kortsiktigt mal. "
        "Skicka BARA de falt som andras."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "vision": {"type": "string", "description": "Meningen med traningen — varfor tranar atleten? T.ex. 'Leva aktivt och prestera i Ironman'"},
            "season_goal": {"type": "string", "description": "Sasongmal — konkret mal for sassongen. T.ex. 'Ironman Kalmar under 10:00, augusti 2026'"},
            "short_term_goal": {"type": "string", "description": "Kortsiktigt mal — fokus kommande 4-6 veckor. T.ex. 'Bygga lopvolym till 45km/vecka utan skador'"},
        },
    },
}

PLAN_SESSIONS_TOOL = {
    "name": "plan_training_sessions",
    "description": (
        "Spara traningspass i atletens plan. Anvand detta nar atleten GODKANNER en veckoplan "
        "eller nar atleten ber dig justera planen. Spara ALDRIG utan att atleten bekraftat. "
        "Presentera forslaget forst, fraga 'Ska jag lagga in det?', och spara forst vid ja. "
        "Inkludera vilopass. Planera 7-10 dagar framat."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sessions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD"},
                        "sport": {"type": "string", "description": "Lopning/Cykel/Sim/Styrka/Brick/Vila"},
                        "title": {"type": "string", "description": "Kort titel, t.ex. 'Lop 50min Z2' eller 'Vila'"},
                        "details": {"type": "string", "description": "Zoninfo, intervaller, puls/watt-granser"},
                        "purpose": {"type": "string", "description": "Kort syfte, t.ex. 'Bygga aerob bas'"},
                    },
                    "required": ["date", "sport", "title"],
                },
            },
        },
        "required": ["sessions"],
    },
}

UPDATE_ZONES_TOOL = {
    "name": "update_athlete_zones",
    "description": (
        "Uppdatera atletens nyckeltal/zoner. Anvand detta nar du foreslar justerade "
        "troskelvarden baserat pa traningsdata. Skicka BARA de varden du vill andra."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ftp": {"type": "integer", "description": "Ny FTP i watt"},
            "css_per_100m": {"type": "string", "description": "Ny CSS, t.ex. '1:42'"},
            "threshold_pace": {"type": "string", "description": "Ny troskelfart, t.ex. '4:20'"},
            "threshold_hr": {"type": "integer", "description": "Ny troskelpuls i bpm"},
            "max_hr": {"type": "integer", "description": "Ny max puls i bpm"},
        },
    },
}

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


def _build_system_prompt(profile: dict | None, activities: list[dict] | None = None,
                         coach_memories: list[dict] | None = None,
                         current_plan: list[dict] | None = None) -> str:
    template = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    now = datetime.now()
    template = (
        template
        .replace("{TODAY_DATE}", now.strftime("%Y-%m-%d"))
        .replace("{TODAY_WEEKDAY}", WEEKDAYS_SV[now.weekday()])
    )

    # Athlete profile
    if profile:
        p = db.profile_to_dict(profile)
        lines = [f"- {k}: {v}" for k, v in p.items() if v]
        template = template.replace("{ATHLETE_PROFILE}", "\n".join(lines))
    else:
        template = template.replace("{ATHLETE_PROFILE}", "Ingen profil tillganglig.")

    # Strava activities
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

    # --- Append coaching knowledge base ---
    try:
        coaching_kb = COACHING_KB_FILE.read_text(encoding="utf-8")
        template += "\n\n" + coaching_kb
    except Exception:
        pass

    # --- Append phase instructions ---
    try:
        phases = PHASES_FILE.read_text(encoding="utf-8")
        template += "\n\n" + phases
    except Exception:
        pass

    # --- Inject athlete goals ---
    if profile:
        goal_lines = []
        if profile.get("vision"):
            goal_lines.append(f"- Vision: {profile['vision']}")
        if profile.get("season_goal"):
            goal_lines.append(f"- Sasongmal: {profile['season_goal']}")
        if profile.get("short_term_goal"):
            goal_lines.append(f"- Kortsiktigt mal: {profile['short_term_goal']}")
        if goal_lines:
            template += "\n\n## Atletens mal\n" + "\n".join(goal_lines)
            template += "\nReferera till dessa mal nar du analyserar och planerar. Varje veckoplan ska motiveras mot malen."
        else:
            template += "\n\n## Atletens mal\nInga mal satta annu. Fraga atleten om vision, sasongmal och kortsiktigt mal tidigt i samtalet."

    # --- Inject athlete zones/key metrics ---
    if profile:
        zone_lines = []
        if profile.get("ftp"):
            zone_lines.append(f"- FTP: {profile['ftp']}W")
        if profile.get("css_per_100m"):
            zone_lines.append(f"- CSS: {profile['css_per_100m']}/100m")
        if profile.get("threshold_pace"):
            zone_lines.append(f"- Troskelfart: {profile['threshold_pace']}/km")
        if profile.get("threshold_hr"):
            zone_lines.append(f"- Troskelpuls: {profile['threshold_hr']} bpm")
        if profile.get("max_hr"):
            zone_lines.append(f"- Max puls: {profile['max_hr']} bpm")
        if zone_lines:
            template += "\n\n## Atletens nyckeltal\n" + "\n".join(zone_lines)
            template += "\nAnvand dessa varden for att berakna exakta zoner i alla pass."

    # --- Inject current training plan ---
    if current_plan:
        plan_lines = []
        for s in current_plan:
            line = f"- {s.get('date','')}: {s.get('title','')} ({s.get('sport','')})"
            if s.get("details"):
                line += f" — {s['details']}"
            plan_lines.append(line)
        if plan_lines:
            template += "\n\n## Aktuell traningsplan (kommande 10 dagar)\n"
            template += "Denna plan ar SATT och godkand av atleten. Andra den INTE utan att fraga.\n"
            template += "\n".join(plan_lines)

    # --- Inject coach memory (relational observations) ---
    if coach_memories:
        mem_lines = []
        for m in coach_memories[:10]:
            conf = m.get("confidence", 0)
            seen = m.get("times_seen", 1)
            mem_lines.append(f"- [{m.get('category','')}] {m.get('observation','')} (sett {seen}x, konfidens {conf:.0%})")
        if mem_lines:
            template += "\n\n## Coachens minnesanteckningar om atleten\n"
            template += "Dessa ar saker du observerat over tid. Anvand dem aktivt i dina svar.\n"
            template += "\n".join(mem_lines)

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


@app.post("/api/profile")
async def update_profile(request: Request):
    uid, token = _get_auth(request)
    body = await request.json()
    # Whitelist allowed fields
    allowed = {
        "experience_level", "age", "weight_kg", "years_training",
        "ironman_finishes", "weekly_hours", "next_race_name",
        "next_race_date", "health_notes", "goal", "notes",
    }
    fields = {k: v for k, v in body.items() if k in allowed and v is not None}
    if fields:
        db.update_profile(uid, token, fields)
    return {"ok": True}


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

    # Build system prompt from profile + activities + coach memory + plan
    profile = db.get_profile(uid, token)
    try:
        activities = db.get_recent_strava_activities(uid, token, days=60)
    except Exception:
        activities = None
    try:
        coach_memories = db.get_coach_memories(uid, token)
    except Exception:
        coach_memories = None
    # Get current plan for next 10 days
    current_plan = None
    try:
        from_d = datetime.now().strftime("%Y-%m-%d")
        to_d = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
        current_plan = db.get_planned_sessions(uid, token, from_d, to_d)
    except Exception:
        pass
    system_prompt = _build_system_prompt(profile, activities, coach_memories, current_plan)

    # Prepare messages (strip _images from history to avoid sending base64 twice)
    clean = []
    for m in req.history[-MAX_HISTORY:]:
        clean.append({"role": m["role"], "content": m.get("content", "")})

    # Build user message — multimodal if images attached
    if req.images:
        content_blocks = []
        for img in req.images[:4]:
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": img.base64,
                },
            })
        if req.message:
            content_blocks.append({"type": "text", "text": req.message})
        clean.append({"role": "user", "content": content_blocks})
    else:
        clean.append({"role": "user", "content": req.message})

    api_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    tools = [WORKOUT_TOOL, PLAN_SESSIONS_TOOL, UPDATE_ZONES_TOOL, SET_GOALS_TOOL] if can_use_feature(tier, "workout_export") else None

    # Non-streaming when tools enabled (to handle tool_use blocks)
    if tools:
        response_obj = api_client.messages.create(
            model=MODEL, max_tokens=2048, system=system_prompt,
            messages=clean, tools=tools,
        )
        text_parts = []
        workout_data = None
        zones_update = None
        goals_update = None
        plan_saved = False
        for block in response_obj.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use" and block.name == "create_workout_file":
                workout_data = block.input
            elif block.type == "tool_use" and block.name == "update_athlete_zones":
                zones_update = block.input
            elif block.type == "tool_use" and block.name == "set_athlete_goals":
                goals_update = block.input
                try:
                    fields = {k: v for k, v in goals_update.items() if v}
                    if fields:
                        fields["goal_updated_at"] = datetime.now().isoformat()
                        db.update_profile(uid, token, fields)
                except Exception as e:
                    print(f"Goals save error: {e}")
            elif block.type == "tool_use" and block.name == "plan_training_sessions":
                try:
                    db.upsert_planned_sessions_batch(uid, block.input.get("sessions", []))
                    plan_saved = True
                    print(f"[plan] Saved {len(block.input.get('sessions', []))} sessions")
                except Exception as e:
                    print(f"Plan save error: {e}")

        response_text = "\n".join(text_parts)
        _save_conv(uid, token, req.history, req.message, response_text)
        result = {"text": response_text}
        if workout_data:
            result["workout"] = workout_data
        if zones_update:
            result["zones_update"] = zones_update
        if goals_update:
            result["goals_update"] = goals_update
        if plan_saved:
            result["plan_saved"] = True
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
    # Fire-and-forget: write coach memory observations
    import threading
    threading.Thread(
        target=_write_memory, args=(uid, message, response), daemon=True
    ).start()


def _write_memory(user_id: str, user_msg: str, coach_response: str):
    """Background task: analyze conversation and save observations to coach_memory."""
    try:
        api_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        system = """Analysera detta coachsamtal och identifiera observationer varda att minnas.
Svara ENBART med ett JSON-array. Inga kommentarer utanfor JSON.
Om inget ar vart att minnas, svara med tom array: []

[{"category": "behavior|physical|mental|preference", "observation": "kort observation"}]

Kategorier:
- behavior: beteendemonster (t.ex. "kor alltid for hart pa intervaller")
- physical: fysiologi (t.ex. "ont i handleden", "svarar bra pa hogvolym sim")
- mental: psykologi (t.ex. "tvivlar pa sig sjalv", "motiveras av siffror")
- preference: preferenser (t.ex. "vill ha detaljerade planer", "foredrar morrontraning")

Max 2 observationer. Bara saker varda att komma ihag over tid. Tom array om inget sticker ut."""

        msg = f"Atlet: {user_msg[:300]}\nCoach: {coach_response[:500]}"
        response = api_client.messages.create(
            model="claude-haiku-4-20250414",
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": msg}],
        )
        raw = response.content[0].text.strip()
        if not raw or raw == "[]":
            return
        observations = json.loads(raw)
        if observations:
            db.save_memory_observations(user_id, observations)
            print(f"[memory] Saved {len(observations)} observations for {user_id[:8]}")
    except Exception as e:
        print(f"[memory] Error: {e}")


# ── Debug: what does Trixa see? ──────────────────────────────────

@app.get("/api/debug/context")
async def debug_context(request: Request):
    """Show what data Trixa has access to (for debugging)."""
    uid, token = _get_auth(request)
    profile = None
    activities = None
    memories = None
    plan = None
    errors = []

    try:
        profile = db.get_profile(uid, token)
    except Exception as e:
        errors.append(f"profile: {e}")
    try:
        activities = db.get_recent_strava_activities(uid, token, days=60)
    except Exception as e:
        errors.append(f"activities: {e}")
    try:
        memories = db.get_coach_memories(uid, token)
    except Exception as e:
        errors.append(f"memories: {e}")
    try:
        from_d = datetime.now().strftime("%Y-%m-%d")
        to_d = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
        plan = db.get_planned_sessions(uid, token, from_d, to_d)
    except Exception as e:
        errors.append(f"plan: {e}")

    return {
        "profile_fields": db.profile_to_dict(profile) if profile else None,
        "activities_count": len(activities) if activities else 0,
        "memories": memories or [],
        "planned_sessions": len(plan) if plan else 0,
        "errors": errors,
    }


# ── Goals ────────────────────────────────────────────────────────

@app.get("/api/goals")
async def get_goals(request: Request):
    uid, token = _get_auth(request)
    profile = db.get_profile(uid, token)
    if not profile:
        return {"vision": None, "season_goal": None, "short_term_goal": None}
    return {
        "vision": profile.get("vision"),
        "season_goal": profile.get("season_goal"),
        "short_term_goal": profile.get("short_term_goal"),
    }


@app.post("/api/goals")
async def save_goals(request: Request):
    uid, token = _get_auth(request)
    body = await request.json()
    fields = {}
    for k in ("vision", "season_goal", "short_term_goal"):
        if k in body:
            fields[k] = body[k]
    if fields:
        fields["goal_updated_at"] = datetime.now().isoformat()
        db.update_profile(uid, token, fields)
    return {"ok": True}


# ── Athlete Zones ────────────────────────────────────────────────

@app.get("/api/athlete/zones")
async def get_zones(request: Request):
    uid, token = _get_auth(request)
    try:
        profile = db.get_profile(uid, token)
    except Exception:
        return {"zones": None}
    if not profile:
        return {"zones": None}
    return {"zones": {
        "ftp": profile.get("ftp"),
        "css_per_100m": profile.get("css_per_100m"),
        "threshold_pace": profile.get("threshold_pace"),
        "threshold_hr": profile.get("threshold_hr"),
        "max_hr": profile.get("max_hr"),
    }}


@app.post("/api/athlete/zones")
async def save_zones(request: Request):
    uid, token = _get_auth(request)
    body = await request.json()
    fields = {}
    for k in ("ftp", "css_per_100m", "threshold_pace", "threshold_hr", "max_hr"):
        if k in body and body[k] is not None:
            fields[k] = body[k]
    if fields:
        db.update_profile(uid, token, fields)
    return {"ok": True}


# ── Weekly Plan Status ───────────────────────────────────────────

@app.get("/api/plan/status")
async def plan_status(request: Request):
    """Return rolling 7-day plan from planned_sessions + Strava overlay."""
    uid, token = _get_auth(request)

    from datetime import datetime, timedelta
    DAYS_SHORT = ["Man", "Tis", "Ons", "Tor", "Fre", "Lor", "Son"]
    today = datetime.now()
    from_date = today.strftime("%Y-%m-%d")
    to_date = (today + timedelta(days=6)).strftime("%Y-%m-%d")

    # Get planned sessions from DB
    try:
        planned = db.get_planned_sessions(uid, token, from_date, to_date)
    except Exception:
        planned = []

    # Get Strava activities for today (in case already trained)
    try:
        activities = db.get_recent_strava_activities(uid, token, days=1)
    except Exception:
        activities = []

    # Build day-by-day
    days = []
    has_plan = len(planned) > 0
    for i in range(7):
        d = today + timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        day_short = DAYS_SHORT[d.weekday()]
        is_today = (i == 0)

        # Find planned sessions for this day
        day_plans = [p for p in planned if p.get("date") == date_str]
        if day_plans:
            plan_title = " + ".join(p.get("title", "") for p in day_plans)
            plan_details = day_plans[0].get("details", "")
            plan_purpose = day_plans[0].get("purpose", "")
        else:
            plan_title = ""
            plan_details = ""
            plan_purpose = ""

        # Find actual activities
        day_activities = [a for a in activities if a.get("date") == date_str]
        actual_summary = ""
        if day_activities:
            parts = []
            for a in day_activities:
                p = [a.get("type", "")]
                if a.get("duration_min"): p.append(f"{int(a['duration_min'])}min")
                if a.get("distance_km"): p.append(f"{a['distance_km']}km")
                parts.append(" ".join(p))
            actual_summary = " + ".join(parts)

        # Color status
        if day_activities:
            is_rest = plan_title and ("vila" in plan_title.lower())
            status = "yellow" if is_rest else "green"
        elif is_today:
            status = "today"
        else:
            status = "future"

        days.append({
            "day": day_short,
            "date": date_str,
            "planned": plan_title,
            "details": plan_details,
            "purpose": plan_purpose,
            "actual": actual_summary,
            "status": status,
            "is_today": is_today,
        })

    return {"days": days, "has_plan": has_plan}


# ── Coach Brief (for dashboard) ──────────────────────────────────

@app.get("/api/coach/brief")
async def coach_brief(request: Request):
    """Generate Trixa's current analysis for the dashboard 'Tank pa' section.
    Returns a short coaching nudge based on recent activities + coach memory.
    Cached per user per day to avoid repeated API calls.
    """
    uid, token = _get_auth(request)

    # Check if we have a cached brief from today
    try:
        cached = db.get_coach_brief(uid, token)
        if cached:
            return {"brief": cached["brief"], "follow_up": cached.get("follow_up")}
    except Exception:
        pass

    # Build context from Strava + profile
    profile = db.get_profile(uid, token)
    try:
        activities = db.get_recent_strava_activities(uid, token, days=14)
    except Exception:
        activities = []

    # Get coach memory observations
    try:
        memories = db.get_coach_memories(uid, token)
    except Exception:
        memories = []

    if not activities and not memories:
        return {"brief": "Koppla Strava eller chatta med mig sa jag kan lara kanna dig!", "follow_up": None}

    # Build a compact context for Claude
    act_lines = []
    for a in (activities or [])[:10]:
        parts = [f"{a['date']}: {a['type']}"]
        if a.get('duration_min'): parts.append(f"{a['duration_min']}min")
        if a.get('distance_km'): parts.append(f"{a['distance_km']}km")
        if a.get('pace'): parts.append(a['pace'])
        if a.get('avg_hr'): parts.append(f"puls {a['avg_hr']}")
        act_lines.append(", ".join(parts))

    mem_lines = [f"- [{m.get('category','')}] {m.get('observation','')}" for m in (memories or [])[:5]]

    name = ""
    if profile:
        name = profile.get("name") or profile.get("display_name") or ""

    now = datetime.now()
    weekday = ["mandag","tisdag","onsdag","torsdag","fredag","lordag","sondag"][now.weekday()]

    api_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    system = f"""Du ar Trixa, personlig tranare. Skriv en kort coachanalys (max 3 meningar) baserat pa atletens senaste traning.

Regler:
- Var direkt, varm, aldrig fluffig
- Referera till faktisk data (pass, puls, fart)
- Ge ETT konkret rad for kommande dagar
- Om du ser ett monster (t.ex. for hard traning), namna det
- Om det ar relevant, lagg till en uppfoljning: "Aterkommer pa [dag]"
- Svara pa svenska
- Tilltala atleten vid namn om du vet det

Idag ar {weekday} {now.strftime('%Y-%m-%d')}."""

    user_msg = f"""Atlet: {name}

Senaste 14 dagars traning:
{chr(10).join(act_lines) if act_lines else 'Ingen data'}

Minnesanteckningar om atleten:
{chr(10).join(mem_lines) if mem_lines else 'Inga anteckningar annu'}

Skriv en kort dashboardanalys (max 3 meningar + eventuell uppfoljningsdag)."""

    try:
        response = api_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        brief_text = response.content[0].text

        # Try to extract follow-up day
        follow_up = None
        import re
        fu_match = re.search(r'[Aa]terkommer?\s+(?:pa\s+)?(\w+dag)', brief_text)
        if fu_match:
            follow_up = fu_match.group(1).capitalize()

        # Cache the brief
        try:
            db.save_coach_brief(uid, brief_text, follow_up)
        except Exception:
            pass

        return {"brief": brief_text, "follow_up": follow_up}
    except Exception as e:
        print(f"Coach brief error: {e}")
        return {"brief": "Jag analyserar din traning...", "follow_up": None}


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
    try:
        ok, msg = db.apply_discount_code(uid, req.code)
        if ok:
            return {"ok": True, "message": msg}
        raise HTTPException(400, msg)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Discount error: {e}")
        raise HTTPException(500, f"Fel vid rabattkod: {str(e)}")


# ── Stripe Payments ──────────────────────────────────────────────

STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_MONTHLY = os.environ.get("STRIPE_PRICE_MONTHLY", "")
STRIPE_PRICE_YEARLY = os.environ.get("STRIPE_PRICE_YEARLY", "")


@app.post("/api/stripe/checkout")
async def create_checkout(request: Request):
    """Create a Stripe Checkout session for subscription."""
    if not STRIPE_SECRET:
        raise HTTPException(503, "Betalning ej konfigurerad")

    uid, _ = _get_auth(request)
    body = await request.json()
    plan = body.get("plan", "monthly")  # monthly or yearly

    import stripe
    stripe.api_key = STRIPE_SECRET

    price_id = STRIPE_PRICE_YEARLY if plan == "yearly" else STRIPE_PRICE_MONTHLY
    if not price_id:
        raise HTTPException(400, "Prisplan saknas")

    base_url = str(request.base_url).rstrip("/")
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{base_url}/?payment=success",
            cancel_url=f"{base_url}/?payment=cancel",
            client_reference_id=uid,
            metadata={"user_id": uid, "plan": plan},
        )
        return {"url": session.url}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events."""
    if not STRIPE_SECRET or not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(503, "Webhooks ej konfigurerade")

    import stripe
    stripe.api_key = STRIPE_SECRET

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, f"Webhook error: {e}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("client_reference_id") or session.get("metadata", {}).get("user_id")
        stripe_sub_id = session.get("subscription")
        stripe_customer_id = session.get("customer")
        if user_id:
            db.update_subscription(user_id, {
                "tier": "premium",
                "status": "active",
                "stripe_subscription_id": stripe_sub_id,
                "stripe_customer_id": stripe_customer_id,
            })
            print(f"[stripe] User {user_id} upgraded to premium")

    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.updated"):
        sub = event["data"]["object"]
        stripe_sub_id = sub.get("id")
        status = sub.get("status")
        # Find user by stripe subscription id
        user_id = sub.get("metadata", {}).get("user_id")
        if user_id and status in ("canceled", "unpaid", "past_due"):
            db.update_subscription(user_id, {
                "tier": "free",
                "status": status,
            })
            print(f"[stripe] User {user_id} downgraded: {status}")

    return {"ok": True}


@app.post("/api/stripe/portal")
async def stripe_portal(request: Request):
    """Create a Stripe Customer Portal session for managing subscription."""
    if not STRIPE_SECRET:
        raise HTTPException(503, "Betalning ej konfigurerad")

    uid, token = _get_auth(request)
    sub = db.get_subscription(uid, token)
    customer_id = sub.get("stripe_customer_id") if sub else None
    if not customer_id:
        raise HTTPException(400, "Inget aktivt abonnemang")

    import stripe
    stripe.api_key = STRIPE_SECRET

    base_url = str(request.base_url).rstrip("/")
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=base_url,
    )
    return {"url": session.url}


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

@app.get("/api/strava/status")
async def strava_debug():
    """Temporary debug endpoint — remove after verification."""
    cid = os.environ.get("STRAVA_CLIENT_ID", "<NOT SET>")
    has_secret = bool(os.environ.get("STRAVA_CLIENT_SECRET"))
    has_state = bool(os.environ.get("STRAVA_STATE_SECRET"))
    return {"client_id": cid, "has_secret": has_secret, "has_state": has_state}


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

        # Initial sync — 12 months of history for new athletes
        import time
        after = int(time.time()) - 365 * 86400
        raw_activities = get_activities(tokens["access_token"], after=after, max_pages=10)
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
    import time, traceback

    try:
        # Refresh tokens if needed
        strava_tokens = ensure_fresh_token(strava_tokens)
        if strava_tokens.get("_refreshed"):
            db.update_strava_tokens(uid, strava_tokens)

        # Regular sync — 6 months
        after = int(time.time()) - 180 * 86400
        raw = get_activities(strava_tokens["access_token"], after=after)
        parsed = [parse_activity(a) for a in raw]
        count = db.upsert_strava_activities(uid, parsed)
        return {"synced": count}
    except Exception as e:
        print(f"Strava sync error: {traceback.format_exc()}")
        raise HTTPException(500, f"Sync misslyckades: {str(e)}")


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
