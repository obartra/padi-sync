"""Weather and sea conditions for a dive, from Open-Meteo (global, free, no key).

Averaged over the hours the dive spans. Cached on disk so reruns make no requests."""

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import requests

WEATHER = "https://historical-forecast-api.open-meteo.com/v1/forecast"
MARINE = "https://marine-api.open-meteo.com/v1/marine"
BEAUFORT = [0.5, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7]
BEAUFORT_NAMES = [
    "Calm",
    "Light air",
    "Light breeze",
    "Gentle breeze",
    "Moderate breeze",
    "Fresh breeze",
    "Strong breeze",
    "Near gale",
    "Gale",
    "Strong gale",
    "Storm",
    "Violent storm",
    "Hurricane force",
]


def beaufort(ms: float) -> int:
    return next((i for i, lim in enumerate(BEAUFORT) if ms < lim), 12)


def sky(sun_frac: float, low_cloud_pct: float, rain_mm: float) -> str:
    """From sunshine and low cloud, not total cloud: total cloud counts thin high cloud that
    barely dims the sun (checked against a national weather service's measured sunshine)."""
    if rain_mm >= 1:
        return "Rain"
    if sun_frac >= 0.9 and low_cloud_pct < 20:
        return "Sunny"
    if sun_frac >= 0.4:
        return "Partly cloudy"
    return "Mostly cloudy" if low_cloud_pct < 70 else "Overcast"


def compass(deg: float) -> str:
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return pts[int((deg % 360) / 22.5 + 0.5) % 16]


def overlapping_hours(times: list[str], start: datetime, end: datetime) -> list[int]:
    """Indexes of the hourly rows whose hour overlaps the dive (hour h covers h to h+1)."""
    return [
        i
        for i, t in enumerate(times)
        if datetime.fromisoformat(t) <= end and datetime.fromisoformat(t) + timedelta(hours=1) > start
    ]


def _get(url, params, cache: Path):
    # Hash the request: params include time zone names like "Asia/Tokyo", which aren't safe in file names.
    key = cache / (hashlib.sha1((url + json.dumps(params, sort_keys=True)).encode()).hexdigest() + ".json")
    if key.exists():
        return json.loads(key.read_text())
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    cache.mkdir(parents=True, exist_ok=True)
    key.write_text(json.dumps(data))
    return data


def conditions(lat: float, lon: float, start: datetime, end: datetime, tz: str, cache: Path) -> dict:
    day = start.strftime("%Y-%m-%d")
    base = dict(latitude=round(lat, 3), longitude=round(lon, 3), start_date=day, end_date=day, timezone=tz)
    w = _get(
        WEATHER,
        dict(
            base,
            hourly="temperature_2m,cloud_cover,cloud_cover_low,sunshine_duration,wind_speed_10m,wind_direction_10m,precipitation",
            wind_speed_unit="ms",
        ),
        cache,
    )["hourly"]
    m = _get(MARINE, dict(base, hourly="wave_height,sea_surface_temperature,ocean_current_velocity"), cache)["hourly"]
    hours = overlapping_hours(w["time"], start, end)
    avg = lambda series: round(
        sum(series[i] for i in hours if series[i] is not None) / max(1, sum(series[i] is not None for i in hours)), 1
    )
    wind = avg(w["wind_speed_10m"])
    out = {
        "air_temp_c": avg(w["temperature_2m"]),
        "cloud_cover_pct": avg(w["cloud_cover"]),
        "rain_mm": avg(w["precipitation"]),
        "wind_ms": wind,
        "wind_dir": compass(w["wind_direction_10m"][hours[0]]),
        "beaufort": beaufort(wind),
        "sky": sky(avg(w["sunshine_duration"]) / 3600, avg(w["cloud_cover_low"]), avg(w["precipitation"])),
        "wave_height_m": avg(m["wave_height"]),
        "sea_temp_c": avg(m["sea_surface_temperature"]),
        # Model surface current in km/h; coarse, so it is a hint, never the logged value on its own.
        "model_current_kmh": avg(m["ocean_current_velocity"]),
        "source": "Open-Meteo weather and marine models",
    }
    out["sea_state"] = BEAUFORT_NAMES[out["beaufort"]]
    return out
