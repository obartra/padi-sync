"""`padi-sync edit`: all dives' answers in one text file, edited in your editor instead of prompts.

Format (one block per dive; '#' lines are comments):
    == dive 1234567890 | 12 Jan 2025 09:03 | 17.0 m | 49 min ==
    title: Reef and turtles
    weather: Cloudy          # Sunny, Partly Cloudy, Cloudy, Rainy, Windy, Foggy
A blank value means "leave empty". Choices accept the same short forms as the prompts."""

import os
import re
import shutil
import subprocess
from pathlib import Path

from .entry import FIELDS
from .store import HOME, Answers, match_choice, parse_number

HEADER = re.compile(r"^== dive (\d+) \|")


def render(rows, answers: Answers) -> str:
    """rows: [(dive, suggested entry)] from a non-interactive prepare()."""
    out = [
        "# Edit the values, save, and close the editor. Blank = leave empty.",
        "# Choices accept a number, any case, or a unique part (e.g. 'ean32', 'partly').",
        "",
    ]
    for dive, want in rows:
        d = f"dive:{dive.dive_id}"
        out.append(
            f"== dive {dive.dive_id} | {dive.start:%d %b %Y %H:%M} | {dive.max_depth_m} m | "
            f"{round(dive.duration_s / 60)} min =="
        )
        for key, label, _, kw in FIELDS:
            v = answers.get(d, key)
            suggested = v is None and want.get(label) not in (None, "")
            if key == "site":
                v = answers.get(d, "padi_site") or v
            val = want.get(label, "") if v is None else v
            val = str(val).replace("\n", "\\n") if key == "note" else val  # line breaks as a literal \n
            if isinstance(val, float) and val.is_integer():
                val = int(val)
            note = []
            if "choices" in kw:
                note.append(", ".join(kw["choices"]))
            if suggested:
                note.append("suggested")
            out.append(f"{key}: {val}" + (f"    # {'; '.join(note)}" if note else ""))
        out.append("")
    return "\n".join(out)


def parse(text: str) -> tuple[dict, list[str]]:
    """-> ({dive_id: {key: value}}, errors)"""
    kinds = {key: kw for key, _, _, kw in FIELDS}
    values, errors, current = {}, [], None
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#") or not line.strip():
            continue
        m = HEADER.match(line)
        if m:
            current = values.setdefault(m.group(1), {})
            continue
        if current is None or ":" not in line:
            errors.append(f"line {n}: not understood: {line.strip()}")
            continue
        key, raw = line.split(":", 1)
        key, raw = key.strip(), re.sub(r"\s+#.*$", "", raw).strip()
        if key not in kinds:
            errors.append(f"line {n}: unknown field '{key}'")
            continue
        kw = kinds[key]
        if raw == "":
            current[key] = ""
        elif "choices" in kw:
            picked = match_choice(raw, kw["choices"])
            if picked:
                current[key] = picked
            else:
                errors.append(f"line {n}: '{raw}' isn't one of: {', '.join(kw['choices'])}")
        elif kw.get("cast") is float:
            try:
                current[key] = parse_number(raw)
            except ValueError:
                errors.append(f"line {n}: '{raw}' isn't a number")
        else:
            current[key] = raw.replace("\\n", "\n") if key == "note" else raw
    return values, errors


def open_editor(path: Path):
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        subprocess.run(f'{editor} "{path}"', shell=True)
    elif shutil.which("code"):
        subprocess.run(["code", "--wait", str(path)])
    else:
        print("Opening in TextEdit. Save, then quit TextEdit (Cmd+Q) to continue.")
        subprocess.run(["open", "-W", "-n", "-t", str(path)])


def edit(rows, answers: Answers):
    path = HOME / "edit-dives.txt"
    path.write_text(render(rows, answers))
    while True:
        open_editor(path)
        values, errors = parse(path.read_text())
        if not errors:
            break
        print("Please fix these, then save and close again:")
        for e in errors:
            print("  " + e)
        path.write_text("# FIX: " + "\n# FIX: ".join(errors) + "\n" + path.read_text())
    for dive_id, fields in values.items():
        scope = f"dive:{dive_id}"
        for key, v in fields.items():
            if key == "site":
                answers.set(scope, "padi_site", v)
            answers.set(scope, key, v)
    print(f"saved answers for {len(values)} dives")
