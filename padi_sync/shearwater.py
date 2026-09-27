"""Read dives from Shearwater Cloud's local database (read-only, from a temporary copy)."""

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_ROOT = (
    Path.home()
    / "Library/Containers/research.shearwater.cloud/Data/Library/Application Support/research.shearwater.cloud/users"
)


@dataclass
class Dive:
    dive_id: str  # stable Shearwater id, used as the idempotency key
    number: int
    start: datetime  # dive computer clock, local time
    duration_s: int
    max_depth_m: float
    avg_depth_m: float | None
    water_min_c: float | None
    water_max_c: float | None
    serial: str

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration_s)


def f_to_c(f):
    return None if f in (None, 0, 0.0) else round((f - 32) * 5 / 9, 1)


def find_db(root: Path = DEFAULT_ROOT) -> Path:
    dbs = [p for p in root.glob("*/dive_data.db") if p.parent.name not in ("loadinguser",)]
    if not dbs:
        raise FileNotFoundError(f"no Shearwater Cloud dive_data.db under {root}")
    return max(dbs, key=lambda p: p.stat().st_mtime)


def load_dives(db: Path | None = None) -> list[Dive]:
    db = db or find_db()
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "dive_data.db"
        shutil.copy(db, copy)  # never open the app's live database
        con = sqlite3.connect(copy)
        rows = con.execute(
            """select d.DiveId, d.DiveNumber, d.DiveDate, d.DiveLengthTime, d.Depth, d.SerialNumber,
                      l.calculated_values_from_samples
               from dive_details d left join log_data l on l.log_id = d.DiveId
               order by d.DiveDate"""
        ).fetchall()
        con.close()
    dives = []
    for dive_id, number, date, length, depth, serial, calc in rows:
        c = json.loads(calc) if calc else {}
        # calculated_values_from_samples is imperial (feet, Fahrenheit); dive_details.Depth is metres.
        dives.append(
            Dive(
                dive_id=str(dive_id),
                number=int(number or 0),
                start=datetime.strptime(date, "%Y-%m-%d %H:%M:%S"),
                duration_s=int(length),
                max_depth_m=round(float(depth), 1),
                avg_depth_m=round(c["AverageDepth"] * 0.3048, 1) if c.get("AverageDepth") else None,
                water_min_c=f_to_c(c.get("MinTemp")),
                water_max_c=f_to_c(c.get("MaxTemp")),
                serial=serial,
            )
        )
    return dives
