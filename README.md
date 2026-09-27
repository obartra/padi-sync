# padi-sync

Fill in your [PADI](https://www.padi.com) online logbook from your Shearwater dive computer. It adds the weather, sea conditions and water current for each dive, and only asks you for what it can't work out. It's safe to run again: it only changes what differs.

```text
$ padi-sync plan --since 2025-01-12
dive 2 (2025-01-12, Reef and turtles): create
    Max Depth: 17.0
    Bottom Time: 49
    Weather: 'Partly Cloudy'
    Air Temperature: 28.8
    Surface Temperature: 29.0
    Current: 'Light'
    ...
```

## What it does

- **Dives:** read from the Shearwater Cloud desktop app's local database (a read-only copy): date, time, max and average depth, and water temperature.
- **Weather and sea:** from [Open-Meteo](https://open-meteo.com) for the hours of each dive: sky, air and sea temperature, wind and waves. Model values are offered as defaults for you to confirm.
- **Footage (optional):** GoPro clips are matched to dives, even when the camera and dive computer clocks disagree. Current is estimated from particles drifting against the reef. It's low confidence, so it's only ever a suggested default.
- **Your answers:** site, buddy, gear, how the dive felt. Each question is asked once, and your answers are offered as defaults for the next dive.
- **Logbook:** created and updated through the learning.padi.com web forms, signed in as you. Each saved entry is read back to check that it stuck.
- **Training dives:** course dives (e.g. Open Water Training Dive 1 to 4) can be logged too, with no dive computer needed.

## Requirements

- macOS with the [Shearwater Cloud](https://shearwater.com/pages/shearwater-cloud) desktop app, synced with your dive computer
- Python 3.11+
- `ffmpeg` / `ffprobe`, only if you want GoPro footage used (`brew install ffmpeg`)

## Getting started

```bash
git clone https://github.com/obartra/padi-sync.git
cd padi-sync
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/playwright install chromium
```

### 1. Your PADI username and password

Set `PADI_EMAIL` and `PADI_PW`, either as environment variables or in `~/.padi-sync/.env`:

```bash
mkdir -p ~/.padi-sync
cat > ~/.padi-sync/.env <<'ENV'
PADI_EMAIL=you@example.com
PADI_PW=your-padi-password
ENV
chmod 600 ~/.padi-sync/.env
```

Environment variables take precedence over the file. To keep the file somewhere else, point `PADI_ENV` at it.

### 2. Sign in once

```bash
.venv/bin/padi-sync login
```

A browser window opens and signs you in. If PADI asks for a code or a captcha, complete it in that window. The session is saved to `~/.padi-sync/session.json`, readable only by you. Run `login` again when it expires.

### 3. Preview, then write

```bash
.venv/bin/padi-sync plan  --since 2025-01-12   # asks what it can't work out, shows the changes, writes nothing
.venv/bin/padi-sync apply --since 2025-01-12   # creates or updates the entries, then verifies them
```

Prefer a form to prompts? `padi-sync edit --since 2025-01-12` opens every dive's answers as one text file in your editor, with suggestions filled in and the valid choices listed. Save and close, and it checks your changes.

Answers are forgiving: the option's number, any case, or a unique part all work (`shorty 3mm`, `ean32`, `partly`), and numbers can have units (`5 kg`, `180bar`). Type `-` to leave a field blank.

### Training dives

```bash
.venv/bin/padi-sync training --course "Open Water Diver" --dive "Open Water Training Dive 1" \
    --site Escambron --date 2025-01-10 --instructor 123456 --depth 8 --time 35 --apply
```

PADI requires an instructor number for training dives, and it asks you to confirm sending the dive to that instructor for verification; the tool confirms it for you. Add `--estimated` if you're entering depth and time from memory; PADI has no field for that, so it's recorded locally.

## Configuration

| Variable | Default | What it's for |
|---|---|---|
| `PADI_EMAIL`, `PADI_PW` | from `~/.padi-sync/.env` | PADI sign-in |
| `PADI_ENV` | `~/.padi-sync/.env` | Where the credentials file is |
| `PADI_SYNC_HOME` | `~/.padi-sync` | Answers, state, session and caches |
| `PADI_SYNC_CLIPS` | GoPro folders on any mounted SD card | Folders with GoPro clips, colon-separated |

Everything the tool stores stays in `~/.padi-sync`: your answers, which PADI entry each dive maps to, the browser session, and cached weather. Nothing is sent anywhere except PADI (your logbook) and Open-Meteo (the date and approximate location of each dive).

## How reruns stay safe

- **Linking:** each dive is tied to its PADI entry in `~/.padi-sync/state.json`, and by a `[padi-sync <dive id>]` tag in the entry's note.
- **Only differences change:** a rerun compares what's on PADI with what it would write, and changes only the differences. It never clears a field you filled in by hand.
- **Other entries are left alone:** an entry with the same title and date but no tag isn't touched.
- **Every save is read back.** Anything PADI didn't store is reported.

## Development

```bash
.venv/bin/pip install -e ".[test]"
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m pytest -q -m "not browser"   # fast unit tests
.venv/bin/python -m pytest -q -m browser         # needs `playwright install chromium`
```

CI runs lint, the unit tests on Python 3.11 and 3.13, and the browser test, as parallel jobs.

## Caveats

- **PADI's website can change.** The tool works through PADI's web pages because there's no public logbook API, so a redesign can break it. The read-back after each save is there to catch that.
- **Estimates are suggestions.** The footage current estimate and the weather model values are offered as defaults for you to confirm, never logged silently.
- **Not affiliated with PADI or Shearwater.**
