"""
main.py — Trixa FastAPI backend.
Serves API endpoints + static frontend.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
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
from core.tools import get_all_tools, WEEKDAYS_SV
from core.tool_handler import ToolResult, process_response

SYSTEM_PROMPT_FILE = ROOT / "prompts" / "system_prompt.md"
COACHING_KB_FILE = ROOT / "prompts" / "coaching_knowledge.md"
PHASES_FILE = ROOT / "prompts" / "phases.md"
DOCS_DIR = ROOT / "documents"

# Key training doctrine docs injected as primary knowledge source.
# Order matters: zones first (most referenced), then session types, then supporting topics.
KEY_DOCS = [
    "3.2 Instruktion för att Beräkna Träningszoner.md",
    "3.1 Typer av Träningspass.md",
    "3.4 Identifiering och Hantering av Överträning.md",
    "3.5 Styrketräning i Olika Träningsfaser för Triathleter.md",
    "3.7 Detaljerad Plan för Näringsintag under Hård Träning.md",
]

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
    domina: bool = False

class IntervalsPushRequest(BaseModel):
    workout: dict

class DiscountRequest(BaseModel):
    code: str


# ── System prompt assembly ───────────────────────────────────────

def _build_system_prompt(profile: dict | None, activities: list[dict] | None = None,
                         coach_memories: list[dict] | None = None,
                         current_plan: list[dict] | None = None) -> str:
    template = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    now = datetime.now()
    # Build date lookup table for next 14 days so Trixa never miscalculates weekdays
    date_table_lines = []
    for i in range(14):
        d = now + timedelta(days=i)
        label = "IDAG" if i == 0 else ("IMORGON" if i == 1 else "")
        day_name = WEEKDAYS_SV[d.weekday()]
        date_str = d.strftime("%Y-%m-%d")
        short_date = f"{d.day}/{d.month}"
        entry = f"  {day_name} {short_date} = {date_str}"
        if label:
            entry += f" ({label})"
        date_table_lines.append(entry)
    date_table = "\n".join(date_table_lines)

    template = (
        template
        .replace("{TODAY_DATE}", now.strftime("%Y-%m-%d"))
        .replace("{TODAY_WEEKDAY}", WEEKDAYS_SV[now.weekday()])
    )

    # Inject date table right after the template header
    template += f"\n\n## Datumreferens (ANVAND DENNA — rakna ALDRIG sjalv)\n{date_table}\n"

    # Athlete profile
    if profile:
        p = db.profile_to_dict(profile)
        lines = [f"- {k}: {v}" for k, v in p.items() if v]
        template = template.replace("{ATHLETE_PROFILE}", "\n".join(lines))
    else:
        template = template.replace("{ATHLETE_PROFILE}", "Ingen profil tillganglig.")

    # Strava activities (with ratings)
    if activities:
        lines = []
        rated_summary = {}
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
            if a.get("rating"):
                parts.append(f"betyg: {a['rating']}/5")
                if a.get("rating_comment"):
                    parts.append(f"kommentar: \"{a['rating_comment']}\"")
                t = a["type"]
                if t not in rated_summary:
                    rated_summary[t] = []
                rated_summary[t].append(a["rating"])
            lines.append(", ".join(parts))
        if rated_summary:
            lines.append("\nTraningspreferenser (baserat pa betyg):")
            for t, ratings in rated_summary.items():
                avg = sum(ratings) / len(ratings)
                emoji = "👍" if avg >= 4 else "👎" if avg <= 2 else "👌"
                lines.append(f"  {emoji} {t}: snitt {avg:.1f}/5 ({len(ratings)} betygsatta)")
        template = template.replace("{RECENT_ACTIVITIES}", "\n".join(lines))
    else:
        template = template.replace("{RECENT_ACTIVITIES}", "Ingen Strava-koppling eller inga aktiviteter.")

    # Append coaching knowledge base (condensed reference)
    try:
        template += "\n\n" + COACHING_KB_FILE.read_text(encoding="utf-8")
    except Exception:
        pass

    # Append phase instructions (condensed reference)
    try:
        template += "\n\n" + PHASES_FILE.read_text(encoding="utf-8")
    except Exception:
        pass

    # Inject key training doctrine documents as primary knowledge source.
    # These take precedence over general knowledge — see "Kunskapsprioritet" in system prompt.
    doc_sections = []
    for doc_name in KEY_DOCS:
        doc_path = DOCS_DIR / doc_name
        try:
            content = doc_path.read_text(encoding="utf-8").strip()
            doc_sections.append(f"### {doc_name.split('.md')[0]}\n\n{content}")
        except Exception:
            pass
    if doc_sections:
        template += "\n\n## TRÄNARLÄRA (PRIMÄR KÄLLA)\n\n" + "\n\n---\n\n".join(doc_sections)

    # Inject athlete goals
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

    # Inject athlete zones/key metrics
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

    # Inject current training plan
    if current_plan:
        plan_lines = []
        for s in current_plan:
            d = s.get('date', '')
            weekday = ''
            try:
                parsed = datetime.strptime(d, "%Y-%m-%d")
                weekday = WEEKDAYS_SV[parsed.weekday()]
                today_str = now.strftime("%Y-%m-%d")
                if d == today_str:
                    weekday += " (IDAG)"
                elif d == (now + timedelta(days=1)).strftime("%Y-%m-%d"):
                    weekday += " (IMORGON)"
            except Exception:
                pass
            line = f"- {weekday} {d}: {s.get('title','')} ({s.get('sport','')})"
            if s.get("details"):
                line += f" — {s['details']}"
            plan_lines.append(line)
        if plan_lines:
            template += "\n\n## Aktuell traningsplan (kommande 10 dagar)\n"
            template += "Denna plan ar SATT och godkand av atleten. Andra den INTE utan att fraga.\n"
            template += "\n".join(plan_lines)

    # Inject health data and self-assessment
    if profile:
        import json as _json
        hd = profile.get("health_data") or {}
        if isinstance(hd, str):
            hd = _json.loads(hd)
        if hd:
            hd_lines = []
            trixa_a = hd.get("trixa_assessment")
            for key, val in hd.items():
                if key == "trixa_assessment":
                    continue
                if isinstance(val, dict):
                    hd_lines.append(f"- {key}: {val.get('value', val)} (noterat {val.get('noted_at', '?')})")
                else:
                    hd_lines.append(f"- {key}: {val}")
            if hd_lines:
                template += "\n\n## Extra halsodata (Trixa har noterat)\n" + "\n".join(hd_lines)
            # Self-assessments
            user_a = profile.get("self_assessment")
            if trixa_a or user_a:
                template += "\n\n## Formbedoming\n"
                if user_a:
                    template += f"- Atletens egen bedomning: {user_a}/5\n"
                if trixa_a:
                    template += f"- Trixas bedomning: {trixa_a.get('value', '?')}/5 ({trixa_a.get('timestamp', '')})\n"

    # Inject coach memory (relational observations)
    if coach_memories:
        mem_lines = []
        for m in coach_memories[:10]:
            conf = m.get("confidence", 0)
            seen = m.get("times_seen", 1)
            mem_lines.append(f"- [{m.get('category','')}] {m.get('observation','')} (sett {seen}x, konfidens {conf:.0%})")
        if mem_lines:
            template += "\n\n## Coachens minnesanteckningar om atleten\n"
            template += """Dessa ar saker du VET om atleten. Du MASTE anvanda dem:
- Fraga ALDRIG om nagot som redan star har. Du VET det redan.
- Referera till det du vet: "Med tanke pa din handled..." inte "Har du nagon skada?"
- Om atleten berattat om energidipp — ta hansyn till det i planeringen utan att fraga igen.
- Om du vet preferenser — anvand dem direkt i forslag.
- Ny information fran samtal ADDERAS till minnet, den ERSATTER inte.

"""
            template += "\n".join(mem_lines)

    return template


# ── Helpers ──────────────────────────────────────────────────────

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


@app.get("/api/health")
async def health():
    """Keepalive endpoint — pingas var 5:e dag for att halla Supabase vaken."""
    try:
        client = db.get_client()
        client.table("profiles").select("id").limit(1).execute()
        return {"ok": True, "supabase": "alive"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
            "refresh_token": result.session.refresh_token,
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


@app.post("/api/auth/reset-password")
async def reset_password(request: Request):
    """Send a password reset email via Supabase."""
    try:
        body = await request.json()
        email = body.get("email", "").strip()
        if not email:
            raise HTTPException(400, "E-post krävs")
        client = db.get_client()
        client.auth.reset_password_email(email, {
            "redirect_to": os.environ.get("SITE_URL", "https://trixa.up.railway.app"),
        })
        return {"ok": True, "message": "Återställningslänk skickad"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/auth/update-password")
async def update_password(request: Request):
    """Update password using the recovery access token."""
    try:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "").strip()
        if not token:
            raise HTTPException(401, "Ingen token")
        body = await request.json()
        pw = body.get("password", "")
        if len(pw) < 6:
            raise HTTPException(400, "Lösenordet måste vara minst 6 tecken")
        client = db.get_admin_client()
        # Decode the JWT to get the user id (no extra dependency)
        import base64, json as _json
        parts = token.split(".")
        padded = parts[1] + "=="
        payload = _json.loads(base64.urlsafe_b64decode(padded))
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(401, "Ogiltig token")
        client.auth.admin.update_user_by_id(user_id, {"password": pw})
        return {"ok": True, "message": "Lösenord uppdaterat"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/auth/refresh")
async def refresh_token(request: Request):
    """Refresh an expired access token using refresh_token."""
    try:
        body = await request.json()
        rt = body.get("refresh_token")
        if not rt:
            raise HTTPException(400, "Missing refresh_token")
        client = db.get_client()
        result = client.auth.refresh_session(rt)
        if not result.session:
            raise HTTPException(401, "Refresh failed")
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
            "refresh_token": result.session.refresh_token,
            "tier": tier,
            "is_admin": is_admin,
            "trial_days": trial_days_remaining(sub),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(401, f"Refresh failed: {e}")


# ── Profile ──────────────────────────────────────────────────────

@app.get("/api/profile")
async def get_profile(request: Request):
    uid, token = _get_auth(request)
    return {"profile": db.get_profile(uid, token)}


@app.post("/api/profile")
async def update_profile(request: Request):
    uid, token = _get_auth(request)
    body = await request.json()
    allowed = {
        "experience_level", "age", "weight_kg", "years_training",
        "ironman_finishes", "weekly_hours", "next_race_name",
        "next_race_date", "health_notes", "goal", "notes",
        "gender", "height_cm", "resting_hr", "blood_pressure",
        "medications", "injuries", "self_assessment",
    }
    fields = {k: v for k, v in body.items() if k in allowed and v is not None}
    if fields:
        db.update_profile(uid, token, fields)
    return {"ok": True}


@app.post("/api/profile/health-data")
async def update_health_data(request: Request):
    """Merge keys into profile health_data JSONB."""
    uid, token = _get_auth(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected JSON object")
    db.merge_health_data(uid, token, body)
    return {"ok": True}


# ── Onboarding ───────────────────────────────────────────────────

@app.get("/api/onboarding/status")
async def onboarding_status(request: Request):
    """Return what key info is missing and a level-adapted opening message.

    Frontend uses this to inject Trixa's first message when profile is empty
    or key fields are missing after 14+ days.
    """
    uid, token = _get_auth(request)
    profile = db.get_profile(uid, token)

    if not profile:
        return {
            "needs_onboarding": True,
            "level": None,
            "missing": ["experience_level", "goals", "weekly_hours"],
            "message": (
                "Hej och välkommen! 👋 Jag är Trixa — din personliga AI-tränare. "
                "Kul att du är här! Berätta lite om dig — "
                "vad är det som gör att du vill träna, och hur ser din träning ut idag?"
            ),
        }

    level = profile.get("experience_level")
    has_goals = bool(profile.get("vision") or profile.get("season_goal"))
    has_hours = bool(profile.get("weekly_hours"))
    has_hr_zone = bool(profile.get("threshold_hr"))
    has_power = bool(profile.get("ftp"))
    has_swim = bool(profile.get("css_per_100m"))
    has_pace = bool(profile.get("threshold_pace"))

    missing = []
    if not level:
        missing.append("experience_level")
    if not has_goals:
        missing.append("goals")
    if not has_hours:
        missing.append("weekly_hours")
    if level == "advanced":
        if not has_hr_zone:
            missing.append("threshold_hr")
        if not has_power:
            missing.append("ftp")
        if not has_swim:
            missing.append("css_per_100m")
    elif level == "intermediate":
        if not has_hr_zone:
            missing.append("threshold_hr")
        if not has_pace:
            missing.append("threshold_pace")

    if not missing:
        return {"needs_onboarding": False, "level": level, "missing": [], "message": None}

    # Level-adapted opening message
    name = profile.get("name") or profile.get("display_name") or ""
    greeting = f"Hej {name}! " if name else "Hej! "

    if not level:
        message = (
            f"{greeting}Vad kul att du är här! 😊 "
            "Berätta lite om dig — tränar du idag, eller funderar du på att komma igång? "
            "Det finns inget rätt eller fel svar, jag anpassar mig helt efter dig."
        )
    elif level == "beginner":
        message = (
            f"{greeting}Kul att ha dig här! "
            "Hur ser din vardag ut träningsmässigt just nu? "
            "Jag vill förstå var du är idag så jag kan hjälpa dig framåt — steg för steg."
        )
    elif level == "intermediate":
        message = (
            f"{greeting}Kul att ses igen! "
            "Hur går träningen just nu — har du något mål du siktar mot, "
            "eller vill du bara komma in i bra rutiner?"
        )
    else:  # advanced
        message = (
            f"{greeting}Kul att ha dig ombord! "
            "Berätta lite om din bakgrund — vad har du tävlat i, "
            "och vad siktar du på den här säsongen? "
            "Så bygger vi upp din profil tillsammans."
        )

    return {
        "needs_onboarding": True,
        "level": level,
        "missing": missing,
        "message": message,
    }


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
    current_plan = None
    try:
        from_d = datetime.now().strftime("%Y-%m-%d")
        to_d = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
        current_plan = db.get_planned_sessions(uid, token, from_d, to_d)
    except Exception:
        pass
    system_prompt = _build_system_prompt(profile, activities, coach_memories, current_plan)

    # DominaTrixa persona override
    if req.domina:
        system_prompt += """

## DOMINATRIXA — ALTERNATIV PERSONA (AKTIV)

Du ar nu DominaTrixa. Du ar fortfarande en kompetent tranare som ger korrekt traning,
men din PERSONLIGHET ar helt annorlunda:

- Du HANAR atleten oavsett vad de gor. Sprang de fort? "Soligt, men Kipchoge gor det som uppvarmning."
- Du ar SARKASTISK, IRONISK och OVERSITTARAKTIG — men alltid med GLIMTEN I OGAT.
- Du anvander OVERDRIVEN JAMORELSE med vardseliten: "5:30/km? Min mormor gar fortare till bussen."
- Du ger BACKHANDED COMPLIMENTS: "Inte helt hopplost. For att vara du, alltsa."
- Du IFRАГАSATTER allt: "Vila? Tror du att Ingebrigtsen vilar? Han vilar nar han dor."
- Ton: tanke dig en drill sergeant som hemligt alskar sina rekryter.
- VIKTIGT: Under all ironi maste traningsraden fortfarande vara KORREKT och VALGRUNDAD.
  DominaTrixa ar elak i tonen, aldrig i substansen.
- Anvand garna emojis som 💀😤🫡👑 och korthuggna utrop.
- Om atleten klagar: "Tears make excellent electrolyte replacement. Fortsatt."
"""

    # Prepare messages
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
    # Tools always enabled — plan, profile, goals, zones are core features
    # Workout export to Intervals.icu is gated separately in the tool handler
    tools = get_all_tools()

    # Non-streaming when tools enabled (to handle tool_use blocks)
    if tools:
        messages_for_api = list(clean)
        result = ToolResult()
        max_rounds = 5

        for _round in range(max_rounds):
            response_obj = api_client.messages.create(
                model=MODEL, max_tokens=2048, system=system_prompt,
                messages=messages_for_api, tools=tools,
            )
            tool_results = process_response(response_obj, uid, token, result)

            if response_obj.stop_reason != "tool_use":
                break

            messages_for_api.append({"role": "assistant", "content": response_obj.content})
            messages_for_api.append({"role": "user", "content": tool_results})

        response_text = "\n".join(result.text_parts)
        _save_conv(uid, token, req.history, req.message, response_text)
        return result.to_response()

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
        system = """Analysera detta coachsamtal och spara ALLA viktiga fakta om atleten.
Svara ENBART med ett JSON-array. Inga kommentarer utanfor JSON.
Om inget ar vart att minnas, svara med tom array: []

[{"category": "behavior|physical|mental|preference|fact", "observation": "specifik observation"}]

Kategorier:
- fact: konkreta fakta ("har ont i handleden", "jobbar som IT-konsult", "har barn", "bor i Stockholm")
- physical: fysiologi ("ont i vanster handled sedan 2 veckor", "kanns tung i benen pa morgonen")
- behavior: beteendemonster ("kor alltid for hart pa intervaller", "skippar ofta fredagspass")
- mental: psykologi ("far energidipp pa eftermiddagen", "har hyperfokus-perioder", "motiveras av siffror")
- preference: preferenser ("foredrar morrontraning", "gillar 6x3min framfor 4x4min", "vill ha korta svar")

VIKTIGT:
- Spara SPECIFIKA detaljer, inte vaga sammanfattningar
- "ont i vanster handled sedan mars 2026" ar battre an "har en skada"
- "far energidipp kl 14-15 varje dag" ar battre an "trott pa eftermiddagen"
- Max 3 observationer per samtal. Tom array om inget sticker ut."""

        msg = f"Atlet: {user_msg[:800]}\nCoach: {coach_response[:800]}"
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

    DAYS_SHORT = ["Man", "Tis", "Ons", "Tor", "Fre", "Lor", "Son"]
    today = datetime.now()
    from_date = today.strftime("%Y-%m-%d")
    to_date = (today + timedelta(days=6)).strftime("%Y-%m-%d")

    try:
        planned = db.get_planned_sessions(uid, token, from_date, to_date)
    except Exception:
        planned = []

    try:
        activities = db.get_recent_strava_activities(uid, token, days=1)
    except Exception:
        activities = []

    days = []
    has_plan = len(planned) > 0
    for i in range(7):
        d = today + timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        day_short = DAYS_SHORT[d.weekday()]
        is_today = (i == 0)

        day_plans = [p for p in planned if p.get("date") == date_str]
        # Build list of individual sessions for this day
        sessions_list = []
        for p in day_plans:
            sessions_list.append({
                "title": p.get("title", ""),
                "details": p.get("details", ""),
                "purpose": p.get("purpose", ""),
                "sport": p.get("sport", ""),
                "duration_min": p.get("duration_min"),
            })
        plan_title = " + ".join(s["title"] for s in sessions_list) if sessions_list else ""
        plan_details = sessions_list[0]["details"] if sessions_list else ""
        plan_purpose = sessions_list[0]["purpose"] if sessions_list else ""

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
            "sessions": sessions_list,
            "details": plan_details,
            "purpose": plan_purpose,
            "actual": actual_summary,
            "status": status,
            "is_today": is_today,
        })

    return {"days": days, "has_plan": has_plan}


@app.get("/api/plan/next-strength")
async def next_strength(request: Request):
    """Return the next planned strength session with exercises."""
    uid, token = _get_auth(request)
    from_date = datetime.now().strftime("%Y-%m-%d")
    to_date = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
    try:
        planned = db.get_planned_sessions(uid, token, from_date, to_date)
    except Exception:
        return {"session": None}

    DAYS_SHORT = ["Man", "Tis", "Ons", "Tor", "Fre", "Lor", "Son"]
    for s in planned:
        sport = (s.get("sport") or "").lower()
        title = (s.get("title") or "").lower()
        if "styrka" in sport or "styrka" in title or "strength" in sport:
            d = s.get("date", "")
            weekday = ""
            try:
                parsed = datetime.strptime(d, "%Y-%m-%d")
                weekday = DAYS_SHORT[parsed.weekday()]
            except Exception:
                pass
            return {
                "session": {
                    "date": d,
                    "weekday": weekday,
                    "title": s.get("title"),
                    "details": s.get("details"),
                    "purpose": s.get("purpose"),
                    "exercises": s.get("exercises"),
                }
            }
    return {"session": None}


# ── Exercise Logging ─────────────────────────────────────────────

@app.post("/api/exercise/log")
async def log_exercise(request: Request):
    """Log effort level for a completed exercise in a strength session."""
    uid, token = _get_auth(request)
    body = await request.json()
    name = body.get("exercise_name", "").strip()
    date = body.get("session_date", "")
    effort = body.get("effort")  # 1-4
    if not name or not date or effort not in (-1, 1, 2, 3, 4):
        raise HTTPException(400, "exercise_name, session_date, effort (1-4) required")
    admin = db.get_admin_client()
    # Upsert: update if same user+date+exercise already logged
    existing = (
        admin.table("exercise_logs")
        .select("id")
        .eq("user_id", uid)
        .eq("session_date", date)
        .eq("exercise_name", name)
        .execute()
    )
    data = {
        "user_id": uid,
        "session_date": date,
        "exercise_name": name,
        "effort": effort,
        "sets": body.get("sets"),
        "reps": body.get("reps"),
        "weight_from": body.get("weight_from"),
    }
    if existing.data:
        admin.table("exercise_logs").update({"effort": effort}).eq("id", existing.data[0]["id"]).execute()
    else:
        admin.table("exercise_logs").insert(data).execute()
    return {"ok": True}


@app.get("/api/exercise/logs")
async def get_exercise_logs(request: Request, date: str = ""):
    """Get logged exercises for a given date."""
    uid, token = _get_auth(request)
    if not date:
        raise HTTPException(400, "date parameter required")
    client = db.get_client()
    client.postgrest.auth(token)
    result = (
        client.table("exercise_logs")
        .select("*")
        .eq("user_id", uid)
        .eq("session_date", date)
        .execute()
    )
    return {"logs": {r["exercise_name"]: r["effort"] for r in (result.data or [])}}


# ── Calendar Feed ────────────────────────────────────────────────

import hmac, hashlib

CAL_SECRET = os.environ.get("CALENDAR_SECRET", os.environ.get("STRAVA_STATE_SECRET", "trixa-cal-default"))


def _make_cal_token(user_id: str) -> str:
    """Generate HMAC-based calendar token from user_id."""
    return hmac.new(CAL_SECRET.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:32]


def _verify_cal_token(token: str, user_id: str) -> bool:
    return hmac.compare_digest(token, _make_cal_token(user_id))


@app.get("/api/calendar/token")
async def get_calendar_token(request: Request):
    """Return the user's calendar subscription URL."""
    uid, token = _get_auth(request)
    cal_token = _make_cal_token(uid)
    base_url = os.environ.get("SITE_URL", "https://trixa.up.railway.app")
    return {
        "url": f"{base_url}/api/calendar/{uid}/{cal_token}/trixa.ics",
        "webcal": f"webcal://{base_url.replace('https://', '').replace('http://', '')}/api/calendar/{uid}/{cal_token}/trixa.ics",
    }


@app.get("/api/calendar/{user_id}/{cal_token}/trixa.ics")
async def calendar_feed(user_id: str, cal_token: str):
    """Public iCal feed — no auth headers needed, validated by HMAC token."""
    if not _verify_cal_token(cal_token, user_id):
        raise HTTPException(403, "Invalid calendar token")

    # Use admin client since we don't have a user access token
    admin = db.get_admin_client()
    from_date = datetime.now().strftime("%Y-%m-%d")
    to_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    result = (
        admin.table("planned_sessions")
        .select("*")
        .eq("user_id", user_id)
        .gte("date", from_date)
        .lte("date", to_date)
        .order("date")
        .execute()
    )
    sessions = result.data or []

    # Build iCal
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Trixa//Trixa Coach//SV",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Trixa Träning",
        "X-WR-TIMEZONE:Europe/Stockholm",
    ]

    for s in sessions:
        d = s.get("date", "")
        title = s.get("title", "Träningspass")
        sport = s.get("sport", "")
        details = s.get("details", "")
        purpose = s.get("purpose", "")
        duration = s.get("duration_min", 60)
        exercises = s.get("exercises")

        # Build description
        desc_parts = []
        if purpose:
            desc_parts.append(f"Syfte: {purpose}")
        if details:
            desc_parts.append(details)
        if exercises and isinstance(exercises, list):
            ex_lines = []
            for ex in exercises:
                name = ex.get("name", "?")
                sets = ex.get("sets", "")
                reps = ex.get("reps", "")
                wf = ex.get("weight_from")
                weights = ex.get("weights")
                if weights:
                    parts = []
                    rps = ex.get("reps_per_set", [])
                    for i, w in enumerate(weights):
                        r = rps[i] if i < len(rps) else reps
                        parts.append(f"{w}kg x{r}" if r else f"{w}kg")
                    ex_lines.append(f"  {name}: {' / '.join(parts)}")
                elif sets and reps:
                    w = f" — {wf}kg" if wf else ""
                    ex_lines.append(f"  {name}: {sets}x{reps}{w}")
                else:
                    ex_lines.append(f"  {name}")
            if ex_lines:
                desc_parts.append("Övningar:\\n" + "\\n".join(ex_lines))

        description = "\\n\\n".join(desc_parts).replace("\n", "\\n")

        # Parse date and create event
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except Exception:
            continue

        # Default start time: 07:00 for morning sessions
        start = dt.replace(hour=7, minute=0)
        end = start + timedelta(minutes=int(duration) if duration else 60)
        uid_str = f"trixa-{user_id[:8]}-{d}-{sport}@trixa.app"

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid_str}",
            f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{title}",
            f"DESCRIPTION:{description}",
            f"CATEGORIES:{sport.upper() if sport else 'TRAINING'}",
            "STATUS:CONFIRMED",
            f"DTSTAMP:{datetime.now().strftime('%Y%m%dT%H%M%S')}Z",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")

    from fastapi.responses import Response
    return Response(
        content="\r\n".join(lines),
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=trixa.ics",
            "Cache-Control": "no-cache, must-revalidate",
        },
    )


# ── Coach Brief (for dashboard) ──────────────────────────────────

@app.get("/api/coach/brief")
async def coach_brief(request: Request, refresh: int = 0, domina: int = 0):
    """Generate Trixa's current analysis for the dashboard 'Tank pa' section."""
    uid, token = _get_auth(request)

    if not refresh:
        try:
            cached = db.get_coach_brief(uid, token)
            if cached:
                return {"brief": cached["brief"], "follow_up": cached.get("follow_up")}
        except Exception:
            pass

    profile = db.get_profile(uid, token)
    try:
        activities = db.get_recent_strava_activities(uid, token, days=14)
    except Exception:
        activities = []

    try:
        memories = db.get_coach_memories(uid, token)
    except Exception:
        memories = []

    if not activities and not memories:
        return {"brief": "Koppla Strava eller chatta med mig sa jag kan lara kanna dig!", "follow_up": None}

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
    weekday = WEEKDAYS_SV[now.weekday()]

    plan_today = ""
    try:
        today_str = now.strftime("%Y-%m-%d")
        plan_sessions = db.get_planned_sessions(uid, token, today_str, today_str)
        if plan_sessions:
            plan_today = ", ".join(p.get("title", "") for p in plan_sessions)
    except Exception:
        pass

    yesterday_activity = ""
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    for a in (activities or []):
        if a.get("date") == yesterday_str:
            parts = [a.get("type", "")]
            if a.get("duration_min"): parts.append(f"{int(a['duration_min'])}min")
            if a.get("distance_km"): parts.append(f"{a['distance_km']}km")
            if a.get("pace"): parts.append(a["pace"])
            if a.get("avg_hr"): parts.append(f"puls {a['avg_hr']}")
            if a.get("rating"): parts.append(f"betyg {a['rating']}/5")
            yesterday_activity = ", ".join(parts)
            break

    api_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    domina_extra = ""
    if domina:
        domina_extra = """
PERSONA: Du ar DominaTrixa — en sarkastisk, ironisk, hanande coach med glimten i ogat.
- Hana atletens prestationer: "5:30/km? Min mormor gar fortare till bussen."
- Jamfor med vardseliten: "Kipchoge gor det har som uppvarmning."
- Backhanded compliments: "Inte helt hopplost. For att vara du, alltsa."
- Anvand emojis som 💀😤🫡👑
- Under all ironi MASTE analysen vara KORREKT. DominaTrixa ar elak i tonen, aldrig i substansen.
"""

    system = f"""Du ar {'DominaTrixa' if domina else 'Trixa'}, personlig tranare. Skriv en kort proaktiv coachanalys for dashboarden.
{domina_extra}
Regler:
- Max 3 meningar, direkt och {'sarkastisk' if domina else 'varm'}
- STALL ALDRIG FRAGOR. Inga fragetecken. Bara pastaenden, analys, observationer och {'han' if domina else 'uppmaningar'}.
- Anledning: atleten kan inte svara i denna ruta — den ar enbart for visning.
- Referera till FAKTISK data (typ, puls, fart, distans)
- Om gardasgens pass avvek fran planen — konstatera det
- Papminn om dagens planerade pass om det finns ett
- Om du ser monster (overtraning, for hart, bra trend) — namna det
- Svara pa svenska, tilltala vid namn

Idag ar {weekday} {now.strftime('%Y-%m-%d')}."""

    user_msg = f"""Atlet: {name}

Gardagens pass: {yesterday_activity or 'Vila / inget registrerat'}
Dagens planerade pass: {plan_today or 'Inget planerat'}

Senaste 14 dagars traning:
{chr(10).join(act_lines) if act_lines else 'Ingen data'}

Minnesanteckningar om atleten:
{chr(10).join(mem_lines) if mem_lines else 'Inga anteckningar annu'}

Skriv en kort proaktiv dashboardanalys."""

    try:
        response = api_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        brief_text = response.content[0].text

        follow_up = None
        import re
        fu_match = re.search(r'[Aa]terkommer?\s+(?:pa\s+)?(\w+dag)', brief_text)
        if fu_match:
            follow_up = fu_match.group(1).capitalize()

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
    plan = body.get("plan", "monthly")

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
        status = sub.get("status")
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


# ── Strava (per-user credentials) ────────────────────────────────

def _get_user_strava_creds(uid: str, token: str) -> tuple[str, str]:
    """Get user's own Strava client_id and client_secret from profile."""
    profile = db.get_profile(uid, token)
    cid = (profile or {}).get("strava_client_id", "") or ""
    secret = (profile or {}).get("strava_client_secret", "") or ""
    return cid, secret


@app.post("/api/strava/save-credentials")
async def strava_save_credentials(request: Request):
    """Save user's own Strava API credentials to profile."""
    uid, token = _get_auth(request)
    body = await request.json()
    cid = body.get("client_id", "").strip()
    secret = body.get("client_secret", "").strip()
    if not cid or not secret:
        raise HTTPException(400, "Client ID och Client Secret kravs")
    db.update_profile(uid, token, {
        "strava_client_id": cid,
        "strava_client_secret": secret,
    })
    return {"ok": True}


@app.get("/api/strava/connect")
async def strava_connect(request: Request):
    uid, token = _get_auth(request)
    cid, secret = _get_user_strava_creds(uid, token)
    if not cid or not secret:
        raise HTTPException(400, "Lagg in dina Strava API-nycklar under Profil forst")

    from integrations.strava import get_authorization_url, sign_state
    redirect_uri = str(request.base_url).rstrip("/") + "/api/strava/callback"
    state = sign_state(uid)
    url = get_authorization_url(redirect_uri, state, client_id=cid)
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

    admin = db.get_admin_client()
    profile_row = admin.table("profiles").select("strava_client_id, strava_client_secret").eq("id", user_id).execute()
    cid = ""
    secret = ""
    if profile_row.data:
        cid = profile_row.data[0].get("strava_client_id", "") or ""
        secret = profile_row.data[0].get("strava_client_secret", "") or ""

    redirect_uri = str(request.base_url).rstrip("/") + "/api/strava/callback"

    try:
        tokens = exchange_code(code, redirect_uri, client_id=cid, client_secret=secret)
        db.save_strava_tokens(user_id, tokens)

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

    cid, secret = _get_user_strava_creds(uid, token)
    from integrations.strava import ensure_fresh_token, get_activities, parse_activity
    import time, traceback

    try:
        body = await request.json()
    except Exception:
        body = {}
    months = int(body.get("months", 6))

    try:
        strava_tokens = ensure_fresh_token(strava_tokens, client_id=cid, client_secret=secret)
        if strava_tokens.get("_refreshed"):
            db.update_strava_tokens(uid, strava_tokens)

        after = int(time.time()) - months * 30 * 86400
        max_pages = 10 if months > 6 else 5
        raw = get_activities(strava_tokens["access_token"], after=after, max_pages=max_pages)
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


# ── Workout Ratings ──────────────────────────────────────────────

@app.post("/api/activity/{strava_id}/rate")
async def rate_activity(strava_id: int, request: Request):
    """Rate a completed activity (1-5 stars, optional comment)."""
    uid, _ = _get_auth(request)
    body = await request.json()
    rating = body.get("rating")
    comment = body.get("comment", "")
    if not rating or rating < 1 or rating > 5:
        raise HTTPException(400, "Rating maste vara 1-5")
    admin = db.get_admin_client()
    admin.table("strava_activities").update({
        "rating": rating,
        "rating_comment": comment,
        "rated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("strava_id", strava_id).eq("user_id", uid).execute()
    return {"ok": True}


@app.get("/api/workout-preferences")
async def workout_preferences(request: Request):
    """Get user's workout type preferences based on ratings."""
    uid, token = _get_auth(request)
    client = db.get_client()
    client.postgrest.auth(token)
    result = client.table("strava_activities") \
        .select("type, rating") \
        .eq("user_id", uid) \
        .not_.is_("rating", "null") \
        .execute()
    prefs = {}
    for row in result.data:
        t = row["type"]
        if t not in prefs:
            prefs[t] = {"ratings": [], "type": t}
        prefs[t]["ratings"].append(row["rating"])
    summary = []
    for t, p in prefs.items():
        avg = sum(p["ratings"]) / len(p["ratings"])
        summary.append({
            "type": t,
            "avg_rating": round(avg, 1),
            "count": len(p["ratings"]),
            "liked": sum(1 for r in p["ratings"] if r >= 4),
            "disliked": sum(1 for r in p["ratings"] if r <= 2),
        })
    return {"preferences": sorted(summary, key=lambda x: -x["avg_rating"])}
