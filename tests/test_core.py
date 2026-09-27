"""Run: .venv/bin/python -m pytest -q"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from padi_sync import editfile, entry, footage, gopro, padi, shearwater, weather
from padi_sync.store import Answers, match_choice, parse_number


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PADI_SYNC_HOME", str(tmp_path / "home"))
    import importlib

    import padi_sync.store as st

    importlib.reload(st)
    monkeypatch.setattr("padi_sync.store.HOME", tmp_path / "home")
    return tmp_path / "home"


def make_db(path: Path, dives):
    con = sqlite3.connect(path)
    con.execute("create table dive_details (DiveId, DiveNumber, DiveDate, DiveLengthTime, Depth, SerialNumber)")
    con.execute("create table log_data (log_id, calculated_values_from_samples)")
    for i, (date, secs, depth, calc) in enumerate(dives):
        con.execute("insert into dive_details values (?,?,?,?,?,?)", (100 + i, i + 1, date, secs, depth, "SN"))
        con.execute("insert into log_data values (?,?)", (100 + i, json.dumps(calc)))
    con.commit()
    con.close()


def test_shearwater_units_and_order(tmp_path):
    db = tmp_path / "dive_data.db"
    make_db(
        db,
        [
            ("2025-01-12 10:53:48", 2596, 19.90854, {"AverageDepth": 27.22741, "MinTemp": 82.0, "MaxTemp": 83.0}),
            ("2025-01-12 09:03:11", 2955, 16.98171, {}),
        ],
    )
    dives = shearwater.load_dives(db)
    assert [d.number for d in dives] == [2, 1] or dives[0].start < dives[1].start
    d2 = next(d for d in dives if d.number == 1 and d.max_depth_m == 19.9)
    assert d2.avg_depth_m == 8.3  # 27.2 ft
    assert d2.water_min_c == 27.8 and d2.water_max_c == 28.3
    assert d2.end == datetime(2025, 1, 12, 11, 37, 4)
    assert next(d for d in dives if d.max_depth_m == 17.0).water_min_c is None  # no samples: unknown, not 0


def test_shearwater_reads_a_copy_not_the_live_db(tmp_path):
    db = tmp_path / "dive_data.db"
    make_db(db, [("2025-01-12 09:03:11", 60, 5, {})])
    before = db.stat().st_mtime_ns
    shearwater.load_dives(db)
    assert db.stat().st_mtime_ns == before


def clip(t):
    return gopro.Clip(Path("x"), t, 10)


def test_clock_offset_needs_agreement_across_dives():
    starts = [datetime(2025, 1, 12, 9, 3, 11), datetime(2025, 1, 12, 10, 53, 48), datetime(2025, 1, 12, 13, 58, 28)]
    clips = [
        clip(datetime(2025, 1, 12, 9, 23, 2)),
        clip(datetime(2025, 1, 12, 11, 14, 15)),
        clip(datetime(2025, 1, 12, 14, 18, 23)),
        clip(datetime(2025, 1, 12, 14, 30, 0)),
    ]
    assert gopro.clock_offset(starts, clips) == timedelta(minutes=19, seconds=51)
    # One dive disagrees by 10 minutes: not a clock offset.
    clips[1] = clip(datetime(2025, 1, 12, 11, 24, 15))
    assert gopro.clock_offset(starts, clips) is None


def test_clips_for_uses_offset():
    s, e = datetime(2025, 1, 12, 10, 53, 48), datetime(2025, 1, 12, 11, 37, 4)
    clips = [clip(datetime(2025, 1, 12, 11, 14, 15)), clip(datetime(2025, 1, 12, 12, 0, 0))]
    got = gopro.clips_for(s, e, clips, timedelta(minutes=20))
    assert [c.start for c in got] == [datetime(2025, 1, 12, 11, 14, 15)]


@pytest.mark.parametrize("ms,b", [(0.2, 0), (5.0, 3), (5.7, 4), (7.3, 4), (9.0, 5), (40, 12)])
def test_beaufort(ms, b):
    assert weather.beaufort(ms) == b


def test_sky_and_compass():
    # Real case: model total cloud 78% but low cloud 25% and full sun; measured sunshine said partly sunny.
    assert weather.sky(1.0, 25, 0.4) == "Partly cloudy"
    assert (
        weather.sky(1.0, 10, 0) == "Sunny" and weather.sky(0.1, 80, 0) == "Overcast" and weather.sky(1, 0, 2) == "Rain"
    )
    assert weather.compass(5) == "N" and weather.compass(350) == "N" and weather.compass(180) == "S"


def test_diff_is_idempotent_and_numeric_tolerant():
    want = {"Max Depth": 19.9, "Weather": "Partly Cloudy", "Note": "x"}
    assert padi.diff({"Max Depth": "19.9", "Weather": "Partly Cloudy", "Note": "x"}, want) == {}
    assert padi.diff({"Max Depth": "19.90", "Weather": "Sunny"}, want) == {
        "Weather": ("Sunny", "Partly Cloudy"),
        "Note": (None, "x"),
    }


def test_answers_asked_once_then_cached(monkeypatch):
    calls = []
    monkeypatch.setattr("builtins.input", lambda p: calls.append(p) or "Reef Point")
    a = Answers()
    assert a.ask("dive:1", "site", "site") == "Reef Point"
    assert Answers().ask("dive:1", "site", "site") == "Reef Point"
    assert len(calls) == 1


def test_answers_offer_previous_dive_as_default(monkeypatch):
    a = Answers()
    a.set("dive:1", "suit", "Full Suit 3mm")
    monkeypatch.setattr("builtins.input", lambda p: "")
    assert (
        a.ask("dive:2", "suit", "Suit", choices=["Full Suit 3mm", "Shorty"], fallback_scopes=["dive:1"])
        == "Full Suit 3mm"
    )


def test_answers_no_ask_records_missing():
    a = Answers(interactive=False)
    assert a.ask("dive:1", "buddy", "Buddy") is None
    assert a.missing == ["dive:1.buddy"]


def test_current_classification_thresholds():
    assert footage.classify(2) == "None" and footage.classify(15.4) == "Light"
    assert footage.classify(50) == "Medium" and footage.classify(90) == "Strong"


def test_particle_drift_detects_moving_specks_over_still_bottom():
    rng = np.random.default_rng(0)
    h, w = 540, 960
    bottom = rng.integers(0, 255, (h // 2, w)).astype(np.uint8)  # textured, static

    def frame(shift):
        f = np.full((h, w), 90, np.uint8)
        f[h // 2 :] = bottom
        pts = np.array([[x, y] for x in range(40, w - 40, 60) for y in range(30, h // 2 - 30, 50)])
        for x, y in pts:
            f[y : y + 3, (x + shift) % w : (x + shift) % w + 3] = 250
        return f

    still = footage.pair_drift(frame(0), frame(0))
    moving = footage.pair_drift(frame(0), frame(4))
    assert still is not None and moving is not None and moving > still + 5


def test_login_errors_never_include_the_password(monkeypatch, tmp_path):
    """A failing sign-in step must not surface Playwright's call log, which echoes typed values."""
    secret = "not-a-real-password-123"
    env = tmp_path / ".env.padi"
    env.write_text(f"PADI_PW={secret}\n")
    monkeypatch.setattr(padi, "ENV_FILE", env)

    class Boom(Exception):
        pass

    class FakePage:
        url = "https://account.padi.com"

        def goto(self, *_):
            raise Boom(f'fill("{secret}") timed out')

    class FakeCtx:
        def new_page(self):
            return FakePage()

    class FakeBrowser:
        def new_context(self):
            return FakeCtx()

        def close(self):
            pass

    class FakePW:
        chromium = type("C", (), {"launch": staticmethod(lambda headless: FakeBrowser())})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(padi, "sync_playwright", lambda: FakePW())
    monkeypatch.delenv("PADI_PW", raising=False)
    with pytest.raises(SystemExit) as e:
        padi.login("someone@example.com")
    assert secret not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_weather_cache_handles_timezone_names(tmp_path, monkeypatch):
    calls = []

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": 1}

    monkeypatch.setattr(weather.requests, "get", lambda url, params, timeout: calls.append(1) or R())
    params = {"timezone": "Asia/Tokyo", "latitude": 24.3}
    assert weather._get("https://x.test/v1/f", params, tmp_path / "c") == {"ok": 1}
    assert weather._get("https://x.test/v1/f", params, tmp_path / "c") == {"ok": 1}
    assert len(calls) == 1  # second call served from the cache


def test_weather_hours_cover_every_hour_the_dive_touches():
    times = [f"2025-01-12T{h:02d}:00" for h in range(24)]
    # 09:03:11 to 09:52: only the 09:00 hour (the old code matched none).
    assert weather.overlapping_hours(times, datetime(2025, 1, 12, 9, 3, 11), datetime(2025, 1, 12, 9, 52, 26)) == [9]
    assert weather.overlapping_hours(times, datetime(2025, 1, 12, 10, 53, 48), datetime(2025, 1, 12, 11, 37, 4)) == [
        10,
        11,
    ]


@pytest.mark.parametrize(
    "raw,want",
    [
        ("shorty 3mm", "Shorty"),
        ("SHORTY", "Shorty"),
        ("5", "Shorty"),
        ("full 5", "Full Suit 5mm"),
        ("dry", "Dry Suit"),
        ("semi", "Semi Dry"),
        ("wetsuit", None),
    ],
)
def test_match_choice_suits(raw, want):
    assert match_choice(raw, entry.SUITS) == want


@pytest.mark.parametrize(
    "raw,want", [("ean32", "EANx32"), ("nitrox 32", "EANx32"), ("air", "Air"), ("32", "EANx32"), ("3", "EANx36")]
)
def test_match_choice_gas(raw, want):
    assert match_choice(raw, entry.GASES) == want


def test_match_choice_weather():
    opts = ["Sunny", "Partly Cloudy", "Cloudy", "Rainy", "Windy", "Foggy"]
    assert match_choice("partly", opts) == "Partly Cloudy" and match_choice("cloudy", opts) == "Cloudy"


@pytest.mark.parametrize("raw,want", [("5", 5.0), ("5 kg", 5.0), ("180bar", 180.0), ("17,5 m", 17.5)])
def test_parse_number(raw, want):
    assert parse_number(raw) == want


def test_prompt_takes_number_or_words(monkeypatch):
    replies = iter(["wetsuit", "shorty 3mm"])
    monkeypatch.setattr("builtins.input", lambda p: next(replies))
    assert Answers().ask("dive:1", "suit", "Suit", choices=entry.SUITS) == "Shorty"


def test_edit_file_round_trip_and_validation():
    dive = shearwater.Dive("42", 1, datetime(2025, 1, 12, 9, 3), 2955, 17.0, 11.1, 27.8, 29.4, "SN")
    a = Answers(interactive=False)
    a.set("dive:42", "suit", "Shorty")
    text = editfile.render([(dive, {"Weather": "Cloudy", "Current": "Light"})], a)
    assert "== dive 42 |" in text and "suit: Shorty" in text and "weather: Cloudy" in text and "suggested" in text
    text = text.replace("suit: Shorty", "suit: 5mm full").replace("weight_kg: ", "weight_kg: 5 kg")
    values, errors = editfile.parse(text)
    assert errors == [] and values["42"]["suit"] == "Full Suit 5mm" and values["42"]["weight_kg"] == 5.0
    bad, errors = editfile.parse(text.replace("gas: ", "gas: helium party"))
    assert errors and "gas" not in bad["42"]


def test_edit_file_keeps_slashes_and_line_breaks_in_notes():
    dive = shearwater.Dive("7", 1, datetime(2025, 1, 12, 11, 0), 2596, 19.9, 8.3, 27.8, 28.3, "SN")
    a = Answers(interactive=False)
    a.set("dive:7", "note", "Mantas (a / b).\nSecond line.")
    values, errors = editfile.parse(editfile.render([(dive, {})], a))
    assert errors == [] and values["7"]["note"] == "Mantas (a / b).\nSecond line."


@pytest.mark.browser
def test_choice_tiles_are_matched_by_label_not_internal_value():
    """PADI's tiles store codes ('SomeCurrent', 'Enriched_32') that differ from their labels."""
    from playwright.sync_api import sync_playwright

    tile = (
        '<div class="card-choice"><div class="text"><div>{label}</div></div>'
        '<input type="checkbox" value="{code}"></div>'
    )
    html = (
        "<main><h6 class='heading'>Current</h6><div class='card-choices'>"
        + "".join(
            tile.format(label=label, code=c)
            for label, c in [("None", "NoCurrent"), ("Light", "SomeCurrent"), ("Medium", "MediumCurrent")]
        )
        + "</div></main><script>document.querySelectorAll('.card-choice').forEach(t => t.onclick = () => {"
        "const cb = t.querySelector('input'); cb.checked = !cb.checked; })</script>"
    )
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.set_content(html)
        padi.fill(page, {"Current": (None, "Light")})
        assert page.locator("input[value=SomeCurrent]").is_checked()
        assert padi.read_form(page)["Current"] == "Light"
        with pytest.raises(KeyError):
            padi.fill(page, {"Current": (None, "Ripping")})
        b.close()


def test_credentials_env_vars_override_env_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("PADI_EMAIL=file@example.com\nPADI_PW='from-file'\n# comment\n")
    monkeypatch.delenv("PADI_EMAIL", raising=False)
    monkeypatch.delenv("PADI_PW", raising=False)
    assert padi.read_env(f) == {"PADI_EMAIL": "file@example.com", "PADI_PW": "from-file"}
    monkeypatch.setenv("PADI_PW", "from-env")
    assert padi.read_env(f)["PADI_PW"] == "from-env"
    assert padi.read_env(tmp_path / "missing")["PADI_PW"] == "from-env"


def test_login_explains_missing_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("PADI_EMAIL", raising=False)
    monkeypatch.delenv("PADI_PW", raising=False)
    monkeypatch.setattr(padi, "ENV_FILE", tmp_path / "missing")
    with pytest.raises(SystemExit, match="PADI_EMAIL and PADI_PW"):
        padi.login()
