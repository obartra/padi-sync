"""Build the PADI log entry for one dive: computer data + weather + footage + your answers.

The entry is a flat dict keyed by the PADI form's own labels, so the browser writer and the
diff use exactly what the page shows."""

from datetime import timedelta

import requests

from . import footage, gopro, weather
from .shearwater import Dive
from .store import HOME, Answers, load, save

WAVES = [(0.3, "None"), (0.8, "Small"), (1.5, "Medium")]
SKY_TO_PADI = {
    "Sunny": "Sunny",
    "Partly cloudy": "Partly Cloudy",
    "Mostly cloudy": "Cloudy",
    "Overcast": "Cloudy",
    "Rain": "Rainy",
}
SUITS = ["None", "Full Suit 3mm", "Full Suit 5mm", "Full Suit 7mm", "Shorty", "Semi Dry", "Dry Suit"]
TAG = "[padi-sync {}]"  # written into the note, so a PADI entry can be traced back to its dive


def geocode(name: str) -> tuple[float, float, str] | None:
    r = requests.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": name, "count": 1}, timeout=20)
    res = (r.json().get("results") or [None])[0]
    return (res["latitude"], res["longitude"], res.get("timezone", "UTC")) if res else None


def waves_label(h):
    return next((name for lim, name in WAVES if h < lim), "Large")


def vis_label(m):
    return "High" if m >= 15 else "Average" if m >= 8 else "Low"


GASES = ["Air", "EANx32", "EANx36", "EANx40", "Enriched", "Trimix", "Rebreather"]
GEAR_KEYS = ("suit", "weight_kg", "cylinder", "cylinder_l", "gas")
# Per-dive questions: (answer key, PADI form label, prompt, options). Shared by the prompts and `padi-sync edit`.
FIELDS = [
    ("site", "Dive Site", "Dive site name", {}),
    ("title", "Dive Title", "Dive title", {}),
    ("weather", "Weather", "Weather", {"choices": ["Sunny", "Partly Cloudy", "Cloudy", "Rainy", "Windy", "Foggy"]}),
    ("waves", "Waves", "Waves at the site", {"choices": ["None", "Small", "Medium", "Large"]}),
    ("current", "Current", "Current", {"choices": ["None", "Light", "Medium", "Strong"]}),
    ("visibility_m", "How far could you see?", "Visibility in metres", {"cast": float}),
    ("suit", "Suit", "Suit", {"choices": SUITS}),
    ("weight_kg", "Weight", "Weight (kg)", {"cast": float}),
    ("cylinder", "Cylinder", "Cylinder", {"choices": ["Steel", "Aluminum", "Other"]}),
    ("cylinder_l", "Cylinder size", "Cylinder size (litres)", {"cast": float}),
    ("gas", "Gas Mixture", "Gas", {"choices": GASES}),
    ("start_bar", "Starting Pressure", "Starting pressure (bar)", {"cast": float}),
    ("end_bar", "Ending Pressure", "Ending pressure (bar)", {"cast": float}),
    ("feeling", "Feeling", "How was the dive", {"choices": ["Amazing", "Good", "Average", "Poor"]}),
    ("buddy", "Buddy", "Buddy", {}),
    ("note", "Note", "Anything to add to the note (what you saw, highlights)", {}),
]


def build(
    dive: Dive, answers: Answers, clips: list[gopro.Clip], offset: timedelta | None, prev: list[str] = ()
) -> dict:
    """prev: answer scopes of the earlier dives, most recent first (offered as defaults)."""
    trip = f"trip:{dive.start:%Y-%m-%d}"
    d = f"dive:{dive.dive_id}"
    prev = list(prev)

    site = answers.ask(d, "site", f"Dive {dive.number} on {dive.start:%d %b %Y} at {dive.start:%H:%M}: dive site name")
    area = answers.ask(trip, "area", "Area or island for the weather lookup (e.g. 'Cozumel')")
    loc = answers.get(trip, "latlon")
    if not loc and area:
        g = geocode(area)
        if g:
            answers.set(trip, "latlon", list(g))
            loc = list(g)
    e = {
        "Dive Title": answers.ask(d, "title", "Dive title", default=f"{site or 'Dive'} #{dive.number}"),
        "Dive Site": site,
        "Date": dive.start.strftime("%Y-%m-%d"),
        "Type of Dive": answers.ask(
            trip, "entry", "How did you get in", default="Boat", choices=["Shore", "Boat", "Other"]
        ),
        "Max Depth": dive.max_depth_m,
        "Bottom Time": round(dive.duration_s / 60),
        "Water Type": answers.ask(trip, "water", "Water type", default="Salt", choices=["Salt", "Fresh"]),
        "Body of Water": answers.ask(
            trip, "body", "Body of water", default="Ocean", choices=["Ocean", "Lake", "Quarry", "River", "Other"]
        ),
        "Bottom Temperature": dive.water_min_c,
    }
    notes = []
    if loc:
        w = weather.conditions(loc[0], loc[1], dive.start, dive.end, loc[2], HOME / "cache")
        sky_hint = "Windy" if w["beaufort"] >= 5 else SKY_TO_PADI.get(w["sky"], "Cloudy")
        e["Weather"] = answers.ask(
            d,
            "weather",
            f"Weather (model says {sky_hint}; Enter to accept)",
            default=sky_hint,
            choices=["Sunny", "Partly Cloudy", "Cloudy", "Rainy", "Windy", "Foggy"],
        )
        e["Air Temperature"] = w["air_temp_c"]
        e["Surface Temperature"] = w["sea_temp_c"]
        wave_hint = waves_label(w["wave_height_m"])
        e["Waves"] = answers.ask(
            d,
            "waves",
            f"Waves at the site (offshore model {w['wave_height_m']} m = {wave_hint}; sheltered sites are calmer)",
            default=wave_hint,
            choices=["None", "Small", "Medium", "Large"],
        )
        notes.append(
            f"Weather from {w['source']}: {w['sky'].lower()}, {w['air_temp_c']} °C air, wind {w['wind_dir']} "
            f"{w['wind_ms']} m/s ({w['sea_state'].lower()}), waves {w['wave_height_m']} m, sea {w['sea_temp_c']} °C."
        )

    # Footage: which clips belong to this dive, and a current estimate from them.
    mine = gopro.clips_for(dive.start, dive.end, clips, offset) if offset is not None else []
    est_key = f"current:{dive.dive_id}"
    cache = load("footage.json")
    if mine and est_key not in cache:
        c = footage.estimate_current([c.path for c in mine[:6]])
        cache[est_key] = c.__dict__
        save("footage.json", cache)
    est = cache.get(est_key)
    hint = est["category"] if est and est.get("category") else None
    if hint:
        print(f"  footage suggests current: {hint} ({est['confidence']} confidence, {est['samples']} samples)")
    e["Current"] = answers.ask(
        d, "current", "Current", default=hint, choices=["None", "Light", "Medium", "Strong"], fallback_scopes=[]
    )
    vis = answers.ask(d, "visibility_m", "Visibility in metres", cast=float, fallback_scopes=prev)
    if vis not in (None, ""):
        e["How far could you see?"] = vis
        e["Visibility"] = vis_label(vis)

    # Gear and people: one "same as last dive?" question instead of six, when there is a previous dive.
    last = prev[0] if prev else None
    gear = {k: answers.get(last, k) for k in GEAR_KEYS} if last else {}
    if (
        answers.interactive
        and last
        and any(v not in (None, "") for v in gear.values())
        and all(answers.get(d, k) is None for k in GEAR_KEYS)
    ):
        summary = ", ".join(
            str(v) + (" kg" if k == "weight_kg" else " L" if k == "cylinder_l" else "")
            for k, v in gear.items()
            if v not in (None, "")
        )
        if answers.confirm(f"Same gear as the previous dive ({summary})?"):
            for k, v in gear.items():
                answers.set(d, k, v if v is not None else "")
    for key, label, prompt, kw in FIELDS:
        if key in GEAR_KEYS or key in ("start_bar", "end_bar", "feeling", "buddy"):
            e[label] = answers.ask(d, key, prompt, fallback_scopes=prev if key in GEAR_KEYS + ("buddy",) else (), **kw)
    e["Dive Center"] = answers.ask(trip, "center", "Dive center")

    own = answers.ask(d, "note", "Anything to add to the note (what you saw, highlights)")
    if dive.avg_depth_m:
        notes.insert(
            0, f"Average depth {dive.avg_depth_m} m. Water {dive.water_min_c} to {dive.water_max_c} °C (dive computer)."
        )
    if hint:
        notes.append(
            f"Current {e['Current'].lower() if e['Current'] else 'not set'}"
            f"{' (confirmed)' if answers.get(d, 'current') else ''}; footage estimate: {hint}."
        )
    e["Note"] = "\n".join(([own] if own else []) + notes + [TAG.format(dive.dive_id)])
    return {k: v for k, v in e.items() if v not in (None, "")}
