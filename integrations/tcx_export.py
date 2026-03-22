"""
TCX (Training Center XML) export for Trixa.
Generates .tcx workout files importable by Garmin Connect and TrainingPeaks.
"""

from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom.minidom import parseString


SPORT_MAP = {
    "running": "Running",
    "biking": "Biking",
    "swimming": "Other",
}


def generate_tcx(workout: dict) -> str:
    """
    Generate a TCX workout file from structured workout data.

    workout = {
        "name": "Sweet Spot 3x8min",
        "sport": "biking",
        "steps": [
            {"type": "warmup", "duration_seconds": 600, "description": "..."},
            {"type": "active", "duration_seconds": 480, "repeats": 3,
             "hr_low": 150, "hr_high": 165, "power_low": 220, "power_high": 240},
            {"type": "rest", "duration_seconds": 240},
            {"type": "cooldown", "duration_seconds": 600},
        ]
    }
    """
    sport = SPORT_MAP.get(workout.get("sport", ""), "Other")

    # Root element
    tcdb = Element("TrainingCenterDatabase")
    tcdb.set("xmlns", "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2")
    tcdb.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")

    workouts_el = SubElement(tcdb, "Workouts")
    workout_el = SubElement(workouts_el, "Workout", Sport=sport)

    name_el = SubElement(workout_el, "Name")
    name_el.text = workout.get("name", "Workout")

    for step in workout.get("steps", []):
        step_type = step.get("type", "active")
        repeats = step.get("repeats", 1)
        duration = step.get("duration_seconds", 0)

        if repeats > 1 and step_type == "active":
            # Interval block with repeats
            repeat_el = SubElement(workout_el, "Step")
            repeat_el.set("xsi:type", "Repeat_t")
            rep_count = SubElement(repeat_el, "Repetitions")
            rep_count.text = str(repeats)

            # Active step
            _add_step(repeat_el, "Active", duration, step)

            # Rest step (if rest duration provided in next step or default 50% of active)
            rest_dur = step.get("rest_seconds", duration // 2)
            if rest_dur > 0:
                rest_step_el = SubElement(repeat_el, "Child")
                rest_step_el.set("xsi:type", "Step_t")
                SubElement(rest_step_el, "StepId").text = "0"
                SubElement(rest_step_el, "Name").text = "Vila"
                SubElement(rest_step_el, "Intensity").text = "Resting"
                dur_el = SubElement(rest_step_el, "Duration")
                dur_el.set("xsi:type", "Time_t")
                SubElement(dur_el, "Seconds").text = str(rest_dur)
                tgt = SubElement(rest_step_el, "Target")
                tgt.set("xsi:type", "None_t")
        else:
            intensity = _intensity_for_type(step_type)
            _add_step(workout_el, intensity, duration, step)

    # Format nicely
    raw_xml = tostring(tcdb, encoding="unicode", xml_declaration=False)
    xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + raw_xml

    try:
        dom = parseString(xml_str)
        return dom.toprettyxml(indent="  ", encoding=None)
    except Exception:
        return xml_str


def _intensity_for_type(step_type: str) -> str:
    if step_type in ("warmup", "cooldown"):
        return "Resting"
    return "Active"


def _add_step(parent: Element, intensity: str, duration_seconds: int, step: dict):
    """Add a single workout step element."""
    step_el = SubElement(parent, "Step")
    step_el.set("xsi:type", "Step_t")
    SubElement(step_el, "StepId").text = "0"

    name = step.get("description", step.get("type", "Step"))
    SubElement(step_el, "Name").text = name[:15]
    SubElement(step_el, "Intensity").text = intensity

    # Duration
    if duration_seconds > 0:
        dur_el = SubElement(step_el, "Duration")
        dur_el.set("xsi:type", "Time_t")
        SubElement(dur_el, "Seconds").text = str(duration_seconds)
    else:
        dur_el = SubElement(step_el, "Duration")
        dur_el.set("xsi:type", "UserInitiated_t")

    # Target (HR or Power zone)
    hr_low = step.get("hr_low")
    hr_high = step.get("hr_high")
    power_low = step.get("power_low")
    power_high = step.get("power_high")

    if hr_low and hr_high:
        tgt = SubElement(step_el, "Target")
        tgt.set("xsi:type", "HeartRate_t")
        zone = SubElement(tgt, "HeartRateZone")
        zone.set("xsi:type", "CustomHeartRateZone_t")
        SubElement(zone, "Low").text = str(hr_low)
        SubElement(zone, "High").text = str(hr_high)
    elif power_low and power_high:
        tgt = SubElement(step_el, "Target")
        tgt.set("xsi:type", "Power_t")
        zone = SubElement(tgt, "PowerZone")
        zone.set("xsi:type", "CustomPowerZone_t")
        SubElement(zone, "Low").text = str(power_low)
        SubElement(zone, "High").text = str(power_high)
    else:
        tgt = SubElement(step_el, "Target")
        tgt.set("xsi:type", "None_t")
