"""
core/tools.py — Claude tool definitions for Trixa.
Each tool is a dict matching the Anthropic tool_use JSON schema.
"""
from __future__ import annotations

from datetime import datetime

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

LOG_TRAINING_TOOL = {
    "name": "log_training_session",
    "description": (
        "Logga ett traningspass i traningsloggen. Anvand detta nar atleten berattar om ett pass "
        "de genomfort, skickar en skärmdump fran Garmin/Strava, eller nar du tolkar passdata fran nagon kalla. "
        "Fyll i sa manga falt som mojligt baserat pa informationen. "
        "Anvand extra_data for information som inte har ett eget falt (t.ex. kadens, simtag, vattentemperatur). "
        "Skriv coach_notes med din analys av passet."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Datum YYYY-MM-DD"},
            "sport": {"type": "string", "enum": ["run", "bike", "swim", "strength", "other"]},
            "title": {"type": "string", "description": "Kort titel, t.ex. 'Troskelintervaller 5x1km'"},
            "duration_min": {"type": "number"},
            "distance_km": {"type": "number"},
            "avg_hr": {"type": "integer"},
            "max_hr": {"type": "integer"},
            "avg_power": {"type": "integer"},
            "normalized_power": {"type": "integer"},
            "pace": {"type": "string", "description": "T.ex. '5:45/km'"},
            "tss": {"type": "number"},
            "rpe": {"type": "integer", "description": "Rate of perceived exertion 1-10"},
            "feeling": {"type": "string", "description": "Kort: bra, tungt, fantastiskt, slitet"},
            "notes": {"type": "string", "description": "Atletens egna kommentarer"},
            "coach_notes": {"type": "string", "description": "Din analys av passet — vad gick bra, vad kan forbattras"},
            "source": {"type": "string", "enum": ["chat", "garmin_screenshot", "strava_screenshot", "manual"]},
            "extra_data": {
                "type": "object",
                "description": "Ovriga datapunkter som ar relevanta men saknar eget falt. T.ex. {\"cadence\": 180, \"swim_strokes\": 42, \"splits\": [\"5:30\", \"5:25\", \"5:20\"]}",
            },
        },
        "required": ["date", "sport"],
    },
}

UPDATE_PROFILE_TOOL = {
    "name": "update_athlete_profile",
    "description": (
        "Uppdatera atletens profil med viktig information som framkommer i samtalet. "
        "Anvand detta nar atleten berattar om sin bakgrund, mal, skador, preferenser, etc. "
        "Spara BARA fakta — inte tolkningar. T.ex. om atleten sager 'jag har gjort 13 Ironman' "
        "spara ironman_finishes=13. Uppdatera tyst utan att fraga — det ar inte en plan, det ar fakta."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "experience_level": {"type": "string", "enum": ["beginner", "intermediate", "advanced"]},
            "age": {"type": "integer"},
            "weight_kg": {"type": "number"},
            "years_training": {"type": "integer", "description": "Antal ar med traning"},
            "ironman_finishes": {"type": "integer"},
            "weekly_hours": {"type": "number", "description": "Tillgangliga traningstimmar per vecka"},
            "next_race_name": {"type": "string", "description": "Nasta tavling, t.ex. 'Ironman Kalmar, 15 aug 2026'"},
            "health_notes": {"type": "string", "description": "Skador, mediciner, begransningar"},
            "goal": {"type": "string", "description": "Overgrippande mal"},
            "strength_program": {"type": "string", "description": "Atletens nuvarande styrkeprogram — kompakt text med ovningar, set, reps och vikt. T.ex. 'Knaboj 3x8 60kg, Marklyft 3x6 80kg, Rodd 3x10 50kg'. Uppdatera efter varje styrkepass."},
            "gender": {"type": "string", "enum": ["man", "kvinna", "annat", "vill ej ange"], "description": "Kon"},
            "height_cm": {"type": "integer", "description": "Langd i cm"},
            "resting_hr": {"type": "integer", "description": "Vilopuls i bpm"},
            "blood_pressure": {"type": "string", "description": "Blodtryck, t.ex. '120/80'"},
            "medications": {"type": "string", "description": "Mediciner atleten tar, t.ex. 'Levaxin 50mcg'"},
            "injuries": {"type": "string", "description": "Aktuella skador eller begransningar, t.ex. 'Ont i vanster kna sedan mars'"},
            "self_assessment": {
                "type": "integer",
                "description": "Trixas bedomning av atletens nuvarande form, 1-5. 1=mycket dalig form, 2=under medel, 3=normal, 4=bra form, 5=toppform. Gor denna bedomning nar du har tillrackligt underlag (traningsdata + samtal). Motivera alltid i chatten.",
            },
            "health_data": {
                "type": "object",
                "description": (
                    "Dynamisk halsodata som Trixa observerar i samtalet. "
                    "Skicka BARA nya/andrade nycklar — de MERGAS med befintlig data (inga befintliga nycklar forsvinner). "
                    "Varje varde ska vara en dict med 'value' (det faktiska vardet) och 'noted_at' (datum YYYY-MM-DD). "
                    "Exempel pa nycklar: 'vo2max', 'laktatvarden', 'somnvanor', 'stressniva', 'kost', "
                    "'menscykel', 'operationer', 'allergier', 'kosttillskott', 'alkoholvanor'. "
                    "Exempel: {\"vo2max\": {\"value\": \"52 ml/kg/min\", \"noted_at\": \"2026-03-29\"}}"
                ),
            },
        },
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
        "ANVAND ZONER for intensitet — inte absoluta pulsvarden. "
        "For lopning: ange hr_zone (en zon) eller hr_zone_low + hr_zone_high (spann). "
        "For cykling: ange power_zone eller power_zone_low + power_zone_high. "
        "Zoner: 1=aterhamtning, 2=aerob bas, 3=tempo, 4=tröskel, 5=VO2max. "
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
                        "description": {"type": "string", "description": "Visas pa klockan, t.ex. 'Tröskel' eller 'Latt jogg'"},
                        "hr_zone": {"type": "integer", "description": "HR-zon 1-5. For lopning/sim."},
                        "hr_zone_low": {"type": "integer", "description": "Nedre HR-zon i spann, t.ex. 2 for Z2-Z4."},
                        "hr_zone_high": {"type": "integer", "description": "Ovre HR-zon i spann, t.ex. 4 for Z2-Z4."},
                        "power_zone": {"type": "integer", "description": "Power-zon 1-5. For cykling."},
                        "power_zone_low": {"type": "integer", "description": "Nedre power-zon i spann."},
                        "power_zone_high": {"type": "integer", "description": "Ovre power-zon i spann."},
                    },
                    "required": ["type", "duration_seconds", "description"],
                },
            },
        },
        "required": ["name", "sport", "steps"],
    },
}


def get_plan_tool() -> dict:
    """Build plan tool with today's date so Trixa knows where she is."""
    today = datetime.now().strftime("%Y-%m-%d")
    weekday = WEEKDAYS_SV[datetime.now().weekday()]
    return {
        "name": "plan_training_sessions",
        "description": (
            f"Spara traningspass i atletens plan. Idag ar {weekday} {today}. "
            "KRITISKT: Datum i sessions MASTE vara exakt YYYY-MM-DD och matcha "
            "EXAKT de dagar du namner i texten. Om du sager 'tisdag 25/3' MASTE date vara "
            f"'2026-03-25' (aret ar {datetime.now().year}). "
            "Dubbelkolla att varje datum stammer med veckodagen. "
            "Du MASTE anropa detta verktyg varje gang en plan godkanns eller andras. "
            "Vid andring: skicka HELA den uppdaterade planen (alla dagar kommande 7-10 dagar), "
            "inte bara det andrade passet. Inkludera vilopass (sport='Vila', title='Vila'). "
            "Vad du sparar ar exakt vad som visas pa Hem-sidan — texten i 'title' "
            "ar det atleten ser. Se till att title matchar det du sager i chatten."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sessions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": f"YYYY-MM-DD. Idag = {today}"},
                            "sport": {"type": "string", "description": "Lopning/Cykel/Sim/Styrka/Brick/Vila"},
                            "title": {"type": "string", "description": "Exakt det som visas pa Hem-sidan. T.ex. 'Styrka 30min + Lop 35min Z2' eller 'Vila'"},
                            "details": {"type": "string", "description": "Zoninfo, intervaller, puls/watt-granser. ALDRIG anvand detta falt for styrkeovningar — de MASTE ligga i exercises-arrayen."},
                            "purpose": {"type": "string", "description": "Kort syfte, t.ex. 'Bygga aerob bas'"},
                            "exercises": {
                                "type": "array",
                                "description": (
                                    "OBLIGATORISK for styrkepass (sport='Styrka'): ALLTID inkludera alla ovningar med exakta varden. Utelamna for lopning/cykel/sim. "
                                    "Valj format baserat pa pass: "
                                    "ENKELT (konstant vikt/reps): ange 'sets', 'reps', 'weight_from'. T.ex. 3 set x 12 reps, 20 kg. "
                                    "PROGRESSIVT (vikt/reps varierar per set): ange 'sets' + 'weights' (lista, en vikt per set) + 'reps_per_set' (lista, en reps per set). "
                                    "T.ex. weights=[40,50,60], reps_per_set=[12,10,8] ger '40kgx12 / 50kgx10 / 60kgx8'. "
                                    "Anvand progressivt format nar atleten ar avancerad eller passet ar periodiserat med okande vikt."
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string", "description": "Ovningsnamn, t.ex. 'Knaboj'"},
                                        "sets": {"type": "integer", "description": "Antal set"},
                                        "reps": {"type": "integer", "description": "Reps per set (enkelt format, eller sekunder om unit='s')"},
                                        "unit": {"type": "string", "description": "Enhet: 'reps' (default) eller 's' for tidsbaserade ovningar (t.ex. Plankan)"},
                                        "weight_from": {"type": "number", "description": "Vikt i kg (enkelt format — samma for alla set, eller startvikt)"},
                                        "weight_to": {"type": "number", "description": "Slutvikt i kg om vikten okar linjart. Utelamna om konstant."},
                                        "weights": {
                                            "type": "array",
                                            "items": {"type": "number"},
                                            "description": "Vikt per set i kg (progressivt format). T.ex. [40, 50, 60] for tre set. Langden maste matcha 'sets'.",
                                        },
                                        "reps_per_set": {
                                            "type": "array",
                                            "items": {"type": "integer"},
                                            "description": "Reps per set (progressivt format). T.ex. [12, 10, 8]. Utelamna om reps ar lika for alla set.",
                                        },
                                        "note": {"type": "string", "description": "Extra instruktion, t.ex. 'kontrollerat ner', 'pausa 2s nere'"},
                                    },
                                    "required": ["name", "sets"],
                                },
                            },
                        },
                        "required": ["date", "sport", "title"],
                    },
                },
            },
            "required": ["sessions"],
        },
    }


def get_all_tools() -> list[dict]:
    """Return all tool definitions for the chat endpoint."""
    return [
        WORKOUT_TOOL,
        get_plan_tool(),
        UPDATE_ZONES_TOOL,
        SET_GOALS_TOOL,
        UPDATE_PROFILE_TOOL,
        LOG_TRAINING_TOOL,
    ]
