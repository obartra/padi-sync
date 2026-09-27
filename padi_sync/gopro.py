"""GoPro clips: timestamps, matching clips to dives, and the camera vs dive computer clock offset."""

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median


@dataclass
class Clip:
    path: Path
    start: datetime  # camera clock, converted to local time
    duration_s: float

    @property
    def end(self):
        return self.start + timedelta(seconds=self.duration_s)


def probe(path: Path, tz: timezone) -> Clip | None:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:format_tags=creation_time",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        fmt = json.loads(out.stdout)["format"]
        start = datetime.fromisoformat(fmt["tags"]["creation_time"].replace("Z", "+00:00"))
        return Clip(path, start.astimezone(tz).replace(tzinfo=None), float(fmt["duration"]))
    except (KeyError, ValueError, json.JSONDecodeError):
        return None


def scan(folders: list[Path], tz: timezone) -> list[Clip]:
    clips = []
    for folder in folders:
        for p in sorted(Path(folder).glob("G[XH]*.MP4")):
            c = probe(p, tz)
            if c:
                clips.append(c)
    return sorted(clips, key=lambda c: c.start)


def clock_offset(dive_starts: list[datetime], clips: list[Clip], window=timedelta(minutes=40)) -> timedelta | None:
    """Camera clock minus dive computer clock.

    The first clip of each dive is the entry (filmed right at the dive start), so for each dive
    take the gap to the first clip within `window` after its start. A consistent gap across
    dives is a clock offset, not a coincidence; return the smallest-gap median."""
    gaps = []
    for s in dive_starts:
        after = [c.start - s for c in clips if timedelta(0) <= c.start - s <= window]
        if after:
            gaps.append(min(after))
    if len(gaps) < 2:
        return None
    m = median(gaps)
    # Require agreement within 2 minutes, otherwise it is not a clock offset.
    if all(abs(g - m) <= timedelta(minutes=2) for g in gaps):
        return min(gaps)
    return None


def clips_for(dive_start: datetime, dive_end: datetime, clips: list[Clip], offset: timedelta) -> list[Clip]:
    return [c for c in clips if dive_start + offset - timedelta(minutes=1) <= c.start <= dive_end + offset]
