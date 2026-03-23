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
                         coach_memories: list[dict] | None = None) -> str:
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

    # Build system prompt from profile + activities + coach memory
    profile = db.get_profile(uid, token)
    try:
        activities = db.get_recent_strava_activities(uid, token, days=60)
    except Exception:
        activities = None
    try:
        coach_memories = db.get_coach_memories(uid, token)
    except Exception:
        coach_memories = None
    system_prompt = _build_system_prompt(profile, activities, coach_memories)

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


# ── Athlete Zones ────────────────────────────────────────────────

@app.get("/api/athlete/zones")
async def get_zones(request: Request):
    uid, token = _get_auth(request)
    profile = db.get_profile(uid, token)
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
    """Return this week's plan with Strava activity overlay + color coding."""
    uid, token = _get_auth(request)

    # Get conversation to find latest plan
    conv = db.get_conversation(uid, token)
    plan_text = None
    if conv:
        for msg in reversed(conv.get("messages", [])):
            if msg.get("role") == "assistant" and any(
                k in (msg.get("content") or "")
                for k in ["VECKOPLAN", "MAN ", "Mandag", "**Man", "**MAN"]
            ):
                plan_text = msg["content"]
                break

    # Get this week's Strava activities
    try:
        activities = db.get_recent_strava_activities(uid, token, days=7)
    except Exception:
        activities = []

    # Build day-by-day status
    from datetime import datetime, timedelta
    DAYS_SV = ["Mandag", "Tisdag", "Onsdag", "Torsdag", "Fredag", "Lordag", "Sondag"]
    DAYS_SHORT = ["Man", "Tis", "Ons", "Tor", "Fre", "Lor", "Son"]
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())

    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        day_name = DAYS_SV[i]
        day_short = DAYS_SHORT[i]
        is_past = d.date() < today.date()
        is_today = d.date() == today.date()

        # Find planned activity for this day
        planned = ""
        if plan_text:
            for line in plan_text.split("\n"):
                lower = line.lower().replace("*", "")
                if day_name.lower() in lower or day_short.lower() in lower.split()[0:1]:
                    planned = line.replace("**", "").strip()
                    # Remove the day prefix
                    for prefix in [day_name, day_short, day_name.upper(), day_short.upper()]:
                        if planned.lower().startswith(prefix.lower()):
                            planned = planned[len(prefix):].strip().lstrip("-:").strip()
                    break

        # Find actual activities for this day
        day_activities = [a for a in activities if a.get("date") == date_str]

        # Determine status color
        status = "future"  # gray
        actual_summary = ""
        if day_activities:
            parts = []
            for a in day_activities:
                p = [a.get("type", "")]
                if a.get("duration_min"): p.append(f"{int(a['duration_min'])}min")
                if a.get("distance_km"): p.append(f"{a['distance_km']}km")
                if a.get("avg_hr"): p.append(f"p{a['avg_hr']}")
                parts.append(" ".join(p))
            actual_summary = " + ".join(parts)

            if not planned or "vila" in planned.lower() or "rest" in planned.lower():
                # Trained on a rest day — yellow
                status = "yellow" if planned and ("vila" in planned.lower()) else "green"
            else:
                status = "green"  # Did something on a planned day
        elif is_past:
            if planned and "vila" not in planned.lower() and "rest" not in planned.lower():
                status = "red"  # Missed a planned workout
            else:
                status = "green"  # Rest day, correctly rested
        elif is_today:
            status = "today"

        days.append({
            "day": day_short,
            "date": date_str,
            "planned": planned,
            "actual": actual_summary,
            "status": status,
            "is_today": is_today,
        })

    return {"days": days, "has_plan": plan_text is not None}


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
