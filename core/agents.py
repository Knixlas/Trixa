"""
core/agents.py — Trixa multi-agent pipeline with asyncio.

Flow:
  R0  Analyst        -> intelligence brief  (sequential, reads DB)
  R1  5x specialists -> parallel (asyncio.gather)
  R2  Program builder -> weekly plan        (sequential, synthesizes)
  R3  Trixa coach    -> final response      (sequential, persona + tone)
  R4  Memory writer  -> saves observations  (fire-and-forget)

Tier gating:
  free  = R2 only (program agent, no specialists)
  basic = R0 + R1 + R2
  pro   = R0 + R1 + R2 + R3 + R4

NOTE: This module is for the advanced agent pipeline (future use).
The current app uses direct Claude chat via main.py.
When activated, call `coach(user_id, question, access_token)`.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import anthropic

from core.db import (
    get_profile,
    get_subscription,
    profile_to_dict,
)
from core.membership import get_user_tier

client = anthropic.AsyncAnthropic()
MODEL = "claude-sonnet-4-5"


# ── Data classes ─────────────────────────────────────────────────

@dataclass
class IntelligenceBrief:
    athlete_summary: str
    recent_load: str
    fitness_trends: str
    upcoming_races: str
    current_injuries: str
    weekly_focus: str
    coach_memory_highlights: str
    user_question: str


@dataclass
class SpecialistReport:
    discipline: str
    recommendations: str


# ── Helper: single API call ──────────────────────────────────────

async def call_claude(system: str, user_msg: str, max_tokens: int = 800) -> str:
    response = await client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return response.content[0].text


# ── R0 — Analyst ────────────────────────────────────────────────

async def run_analyst(user_id: str, access_token: str, user_question: str) -> IntelligenceBrief:
    profile = get_profile(user_id, access_token)
    profile_data = profile_to_dict(profile) if profile else {}

    system = """Du ar en analytiker som sammanstaller data infor ett coachsamtal.
Svara ENBART med ett JSON-objekt.

{
  "athlete_summary": "kort om atleten: erfarenhet, mal, niva",
  "recent_load": "sammanfattning senaste 14 dagars traning",
  "fitness_trends": "form/fatigue-trend",
  "upcoming_races": "nasta tavling och antal veckor kvar",
  "current_injuries": "aktiva skador eller 'Inga aktiva skador'",
  "weekly_focus": "vad denna vecka bor prioritera",
  "coach_memory_highlights": "viktiga observationer om atleten"
}"""

    data_dump = {
        "user_profile": profile_data,
        "question": user_question,
    }

    raw = await call_claude(system, json.dumps(data_dump, ensure_ascii=False, default=str), max_tokens=1500)
    parsed = json.loads(raw)
    parsed["user_question"] = user_question
    return IntelligenceBrief(**parsed)


# ── R1 — Specialists (parallel) ─────────────────────────────────

SPECIALISTS = {
    "sim": "Du ar simcoach specialiserad pa triathlon. Ge max 3 konkreta rekommendationer for simtraning denna vecka. Svara pa samma sprak som atletens fraga.",
    "cykel": "Du ar cykelcoach specialiserad pa Ironman. Ge max 3 konkreta rekommendationer for cykeltraning denna vecka. Svara pa samma sprak som atletens fraga.",
    "lop": "Du ar lopcoach specialiserad pa triathlon-lopning. Ge max 3 konkreta rekommendationer. Svara pa samma sprak som atletens fraga.",
    "styrka": "Du ar styrkecoach for uthallighetsidrottare. Ge max 2 rekommendationer. Svara pa samma sprak som atletens fraga.",
    "rehab": "Du ar idrottsfysioterapeut med fokus pa triathlon. Analysera skaderisker. Rehab trumpar allt. Svara pa samma sprak som atletens fraga.",
}


async def run_specialist(discipline: str, brief: IntelligenceBrief) -> SpecialistReport:
    brief_text = f"""INTELLIGENCE BRIEF
Atlet: {brief.athlete_summary}
Coachen vet: {brief.coach_memory_highlights}
Belastning: {brief.recent_load}
Formtrender: {brief.fitness_trends}
Tavlingar: {brief.upcoming_races}
Skador: {brief.current_injuries}
Veckofokus: {brief.weekly_focus}
Fraga: {brief.user_question}"""
    text = await call_claude(SPECIALISTS[discipline], brief_text)
    return SpecialistReport(discipline=discipline, recommendations=text)


async def run_all_specialists(brief: IntelligenceBrief) -> list[SpecialistReport]:
    tasks = [run_specialist(disc, brief) for disc in SPECIALISTS]
    return await asyncio.gather(*tasks)


# ── R2 — Program builder ────────────────────────────────────────

async def run_program_builder(brief: IntelligenceBrief,
                               reports: list[SpecialistReport] | None) -> str:
    system = """Du ar huvudtranare. Satt ihop en konkret veckoplan (man-son).
Regler: Skador trumpar alltid. Max 2 harda pass/vecka.
Varje dag: Vila/Latt/Medel/Hart + disciplin + syfte + tid.
Svara pa samma sprak som atletens fraga."""

    specs = ""
    if reports:
        specs = "\n\n".join(f"=== {r.discipline.upper()} ===\n{r.recommendations}" for r in reports)
    else:
        specs = "(Inga specialistrekommendationer)"

    msg = f"""Brief:
Veckofokus: {brief.weekly_focus}
Tavlingar: {brief.upcoming_races}
Skador: {brief.current_injuries}
Fraga: {brief.user_question}

Specialister:
{specs}"""
    return await call_claude(system, msg, max_tokens=1500)


# ── R3 — Trixa coach ────────────────────────────────────────────

async def run_trixa_coach(brief: IntelligenceBrief, weekly_plan: str) -> str:
    system = f"""Du ar Trixa — personlig AI-triathloncoach.
Direkt och varm. Aldrig fluffig. Datadriven.
Pratar som en erfaren coach som kanner sin atlet val.

Du kanner din atlet: {brief.athlete_summary}
Du vet: {brief.coach_memory_highlights}

Ditt svar ska:
1. Svara direkt pa atletens fraga
2. Presentera veckoplanen motiverande men arligt
3. Lyfta 1-2 extra viktiga saker
4. Kort personlig kommentar

Svara ALLTID pa samma sprak som atleten skriver pa."""

    msg = f"""Fraga: {brief.user_question}
Veckoplan:
{weekly_plan}
Skador: {brief.current_injuries}
Form: {brief.fitness_trends}"""
    return await call_claude(system, msg, max_tokens=1000)


# ── R4 — Memory writer (fire-and-forget) ────────────────────────

async def run_memory_writer(user_id: str, brief: IntelligenceBrief, final_response: str):
    """Placeholder for memory writer. Requires coach_memory table."""
    # TODO: implement when coach_memory is added to Supabase
    pass


# ── Main pipeline ───────────────────────────────────────────────

async def coach(user_id: str, user_question: str, access_token: str) -> str:
    sub = get_subscription(user_id, access_token)
    tier = get_user_tier(sub, False)

    brief: IntelligenceBrief | None = None
    reports: list[SpecialistReport] | None = None

    if tier == "free":
        profile = get_profile(user_id, access_token)
        p = profile_to_dict(profile) if profile else {}
        brief = IntelligenceBrief(
            athlete_summary=f"{p.get('name', 'Atlet')}",
            recent_load="Ej analyserat (free)",
            fitness_trends="Ej analyserat (free)",
            upcoming_races="Ej analyserat (free)",
            current_injuries="Ej analyserat (free)",
            weekly_focus="Svara pa fragan",
            coach_memory_highlights="Ej tillgangligt (free)",
            user_question=user_question,
        )
        return await run_program_builder(brief, None)

    brief = await run_analyst(user_id, access_token, user_question)
    reports = await run_all_specialists(brief)
    weekly_plan = await run_program_builder(brief, reports)

    if tier == "basic":
        return weekly_plan

    final = await run_trixa_coach(brief, weekly_plan)
    asyncio.create_task(run_memory_writer(user_id, brief, final))
    return final
