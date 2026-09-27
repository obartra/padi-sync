"""Local state in ~/.padi-sync (never committed):
answers.json   your answers, per dive and per trip, so nothing is asked twice
state.json     Shearwater dive id -> PADI log id, and the last values written
session.json   Playwright browser session (cookies/localStorage) from `padi-sync login`
cache/         weather responses and footage estimates
"""

import json
import os
import re
from pathlib import Path

HOME = Path(os.environ.get("PADI_SYNC_HOME", Path.home() / ".padi-sync"))


def _path(name):
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME / name


def load(name: str) -> dict:
    p = _path(name)
    return json.loads(p.read_text()) if p.exists() else {}


def save(name: str, data: dict):
    p = _path(name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, default=str))
    tmp.replace(p)  # atomic, so an interrupted run never corrupts state


def parse_number(raw: str) -> float:
    """'5', '5 kg', '180bar', '17.5 m' -> the number."""
    m = re.search(r"-?\d+(?:[.,]\d+)?", raw)
    if not m:
        raise ValueError(raw)
    return float(m.group().replace(",", "."))


def match_choice(raw: str, choices: list[str]) -> str | None:
    """Accept the option number, any case, or a unique part: '3', 'shorty 3mm', 'ean32', 'partly'."""
    r = raw.strip().lower()
    if r.isdigit() and 1 <= int(r) <= len(choices):
        return choices[int(r) - 1]
    norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())
    for c in choices:
        if norm(c) == norm(r):
            return c
    hits = [c for c in choices if norm(c).startswith(norm(r)) or norm(r).startswith(norm(c))]
    if len(hits) == 1:
        return hits[0]
    words = [w for w in re.split(r"\W+", r) if w]
    for rule in (all, any):  # prefer the option containing every word typed
        hits = [c for c in choices if rule(w in c.lower() for w in words)]
        if len(hits) == 1:
            return hits[0]
    compact = [
        c for c in choices if norm(r) in norm(c) or all(ch in norm(c) for ch in norm(r)) and norm(r)[:3] == norm(c)[:3]
    ]
    return compact[0] if len(compact) == 1 else None


class Answers:
    """Answers keyed by scope ('trip:<date>', 'dive:<id>', 'global'). ask() only prompts once."""

    def __init__(self, interactive: bool = True):
        self.data = load("answers.json")
        self.interactive = interactive
        self.missing: list[str] = []

    def get(self, scope: str, key: str):
        return self.data.get(scope, {}).get(key)

    def set(self, scope: str, key: str, value):
        self.data.setdefault(scope, {})[key] = value
        save("answers.json", self.data)

    def ask(self, scope: str, key: str, prompt: str, default=None, choices=None, cast=str, fallback_scopes=()):
        v = self.get(scope, key)
        if v is not None:
            return v
        for fs in fallback_scopes:  # e.g. reuse the same suit as the previous dive
            if self.get(fs, key) not in (None, ""):
                default = self.get(fs, key)
                break
        if not self.interactive:
            self.missing.append(f"{scope}.{key}")
            return default
        hint = f" [{default}]" if default not in (None, "") else ""
        opts = "  " + "  ".join(f"{i}) {c}" for i, c in enumerate(choices, 1)) if choices else ""
        if opts:
            print(opts)
        while True:
            raw = input(f"{prompt}{hint}: ").strip()
            if not raw and default not in (None, ""):
                raw = str(default)
            if raw in ("-", ""):  # blank or "-": leave empty, don't ask again
                self.set(scope, key, "")
                return ""
            if choices:
                picked = match_choice(raw, choices)
                if not picked:
                    print("  didn't match one option; type its number, or - to skip")
                    continue
                raw = picked
            try:
                value = parse_number(raw) if cast is float else cast(raw)
            except ValueError:
                print("  that isn't a number")
                continue
            self.set(scope, key, value)
            return value

    def confirm(self, prompt: str, default=True) -> bool:
        if not self.interactive:
            return default
        raw = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not raw else raw.startswith("y")
