"""padi-sync: fill in your PADI logbook from your Shearwater dive computer.

  padi-sync login                 sign in once (reads PADI_PW from the env file), saves the session
  padi-sync plan  [--since DATE]  show what would be created or changed; writes nothing (default)
  padi-sync apply [--since DATE]  create missing dives and fill empty or changed fields
  padi-sync edit  [--since DATE]  edit all dives' answers in one text file instead of prompts
  padi-sync read  <log id>        print one PADI entry as the form shows it

Idempotent: each dive is tied to its PADI entry through ~/.padi-sync/state.json and a tag in the
entry's note, so running it again only changes what differs."""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import entry, gopro, padi, shearwater
from .store import Answers, load, save


def default_clip_folders() -> list[Path]:
    """PADI_SYNC_CLIPS (colon-separated folders), else GoPro folders on any mounted SD card."""
    env = os.environ.get("PADI_SYNC_CLIPS")
    if env:
        return [Path(p).expanduser() for p in env.split(":") if p]
    return sorted(Path("/Volumes").glob("*/DCIM/*GOPRO"))


def date_text(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d.day} {d.strftime('%B %Y')}"


def prepare(args, answers: Answers):
    dives = [d for d in shearwater.load_dives() if not args.since or d.start >= datetime.fromisoformat(args.since)]
    folders = [Path(f) for f in (args.clips or default_clip_folders()) if Path(f).exists()]
    tz_hours = answers.ask(
        "global", "tz_offset_h", "Local time zone offset from UTC in hours (e.g. 9 for Japan)", cast=float, default=9
    )
    clips = gopro.scan(folders, timezone(timedelta(hours=float(tz_hours or 0)))) if folders else []
    offset = gopro.clock_offset([d.start for d in dives], clips)
    if offset:
        print(f"GoPro and dive computer clocks differ by {offset} (same on every dive); clips matched accordingly")
    # Every dive the computer has, in order, so "previous dive" means the one actually before it.
    order = [f"dive:{d.dive_id}" for d in shearwater.load_dives()]
    return [
        (d, entry.build(d, answers, clips, offset, prev=order[: order.index(f"dive:{d.dive_id}")][::-1])) for d in dives
    ]


def resolve_site(page, answers: Answers, dive, typed: str) -> str:
    """Map your site name to one of PADI's own dive sites, asking you once per dive."""
    scope = f"dive:{dive.dive_id}"
    chosen = answers.get(scope, "padi_site")
    if chosen is not None or not typed:
        return chosen or typed
    padi.open_new(page)
    padi._open_sections(page)
    options = padi.site_suggestions(page, typed)
    if not options or not answers.interactive:
        return typed
    print(f"PADI dive sites matching '{typed}':")
    for i, o in enumerate(options, 1):
        print(f"  {i}. {o}")
    raw = input(f"Pick a number, or Enter to keep '{typed}': ").strip()
    pick = options[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(options) else typed
    answers.set(scope, "padi_site", pick)
    return pick


def run(args, write: bool):
    answers = Answers(interactive=not args.no_ask)
    planned = prepare(args, answers)
    if answers.missing:
        print("not asked (--no-ask), left blank:", ", ".join(sorted(set(answers.missing))))
    state = load("state.json")
    with padi.browser(headless=not args.show) as ctx:
        page = ctx.new_page()
        if not padi.signed_in(page):
            sys.exit(
                "PADI's logbook didn't load (signed out, or the site is slow right now); "
                "try again in a minute, or run `padi-sync login`"
            )
        for dive, want in planned:
            key = dive.dive_id
            want["Dive Site"] = resolve_site(page, answers, dive, want.get("Dive Site", ""))
            log_id = state.get(key, {}).get("padi_id")
            if log_id and not padi.open_existing(page, log_id):
                log_id = None  # deleted on PADI's side: create again
            if not log_id:
                log_id = padi.find_id(page, want["Dive Title"], date_text(want["Date"]))
                if log_id:
                    padi.open_existing(page, log_id)
            if log_id:
                current = padi.read_form(page)
                tag = entry.TAG.format(key)
                if tag not in current.get("Note", "") and state.get(key, {}).get("padi_id") != log_id:
                    print(
                        f"dive {dive.number}: PADI entry {log_id} has the same title and date but no sync tag; skipping"
                    )
                    continue
            else:
                current = {}
            changes = padi.diff(current, want)
            label = f"dive {dive.number} ({want['Date']}, {want['Dive Title']})"
            if not changes:
                print(f"{label}: up to date (PADI {log_id})")
                continue
            print(f"{label}: {'create' if not log_id else f'update PADI {log_id}'}")
            for k, (old, new) in changes.items():
                print(f"    {k}: {old!r} -> {new!r}" if old not in (None, "") else f"    {k}: {new!r}")
            if not write:
                continue
            if not log_id:
                padi.open_new(page)
            padi.fill(page, changes)
            padi.submit(page, new=not log_id)
            if not log_id:
                log_id = padi.find_id(page, want["Dive Title"], date_text(want["Date"]))
            padi.open_existing(page, log_id)
            after = padi.diff(padi.read_form(page), want)  # verify the save stuck
            state[key] = {"padi_id": log_id, "written": want, "at": datetime.now().isoformat(timespec="seconds")}
            save("state.json", state)
            print(f"    saved as PADI {log_id}" + (f"; NOT stored by PADI: {sorted(after)}" if after else ""))


def log_training(a):
    """Training dives have a title fixed by PADI (the dive's name) and no notes field."""
    title = a.dive
    want = {
        "Dive Site": a.site,
        "Date": a.date,
        "Instructor": a.instructor,
        "Type of Dive": a.entry,
        "Max Depth": a.depth,
        "Bottom Time": a.time,
    }
    with padi.browser(headless=True) as ctx:
        page = ctx.new_page()
        existing = [r for r in padi.list_logs(page) if r["title"] == title and r["kind"] == "training"]
        if existing:
            print(f"{title}: already logged (PADI {existing[0]['id']}, {existing[0]['date']}); nothing to do")
            return
        print(f"{title}: create ({a.course})" + (" [depth/time estimated]" if a.estimated else ""))
        for k, v in want.items():
            print(f"    {k}: {v!r}")
        print(f"    Skills: required list as shown; '{padi.NO_FLEXIBLE}'")
        if not a.apply:
            return
        padi.open_training(page, a.course, a.dive)
        padi.fill(page, {k: (None, v) for k, v in want.items()})
        padi.tick_no_flexible(page)
        # PADI looks the instructor up on the first click and only shows its "send to <instructor>?"
        # confirmation on a later click, once the lookup has returned. Click until it appears.
        dialog = page.get_by_text("Instructor Submission")
        for _ in range(8):
            page.get_by_role("button", name="Log Dive").click()
            try:
                dialog.wait_for(timeout=15_000)
                break
            except Exception:
                continue
        else:
            sys.exit("PADI never asked to confirm the instructor; nothing was saved")
        page.get_by_role("button", name="Submit").click()
        page.wait_for_url(re.compile(r"/\d+/success"), timeout=60_000)
        log_id = int(re.search(r"/(\d+)/success", page.url).group(1))
        padi.goto(page, f"{padi.BASE}/logbook/log-dive/training/update/{log_id}")
        page.wait_for_selector("text=Dive Title", state="attached", timeout=90_000)
        page.wait_for_timeout(1500)
        missing = padi.diff(padi.read_form(page), want)
        state = load("state.json")
        state.setdefault("training", {})[title] = {
            "padi_id": log_id,
            "course": a.course,
            "written": want,
            "estimated_depth_time": a.estimated,
        }
        save("state.json", state)
        print(f"    saved as PADI {log_id}" + (f"; NOT stored: {sorted(missing)}" if missing else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="padi-sync", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login")
    lg.add_argument("--email", help="PADI account email (default: PADI_EMAIL)")
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--since", help="only dives on or after this date (YYYY-MM-DD)")
        p.add_argument("--clips", nargs="*", help="folders with GoPro clips (default: the SD card)")
        p.add_argument("--no-ask", action="store_true", help="don't prompt; leave unknown fields blank")
        p.add_argument("--show", action="store_true", help="show the browser")
    ed = sub.add_parser("edit")
    ed.add_argument("--since", help="only dives on or after this date (YYYY-MM-DD)")
    ed.add_argument("--clips", nargs="*", help="folders with GoPro clips (default: the SD card)")
    tr = sub.add_parser("training", help="log a course training dive (no dive computer data needed)")
    tr.add_argument("--course", required=True, help="as PADI names it, e.g. 'Open Water Diver'")
    tr.add_argument("--dive", required=True, help="as PADI names it, e.g. 'Open Water Training Dive 1'")
    tr.add_argument(
        "--estimated", action="store_true", help="depth/time are estimates (recorded locally; PADI has no field for it)"
    )
    tr.add_argument("--site", required=True)
    tr.add_argument("--date", required=True, help="YYYY-MM-DD")
    tr.add_argument(
        "--instructor", required=True, help="instructor's PADI number (PADI requires it for training dives)"
    )
    tr.add_argument("--entry", default="Shore", choices=["Shore", "Boat", "Other"])
    tr.add_argument("--depth", type=float, required=True, help="max depth in metres")
    tr.add_argument("--time", type=int, required=True, help="bottom time in minutes")
    tr.add_argument("--apply", action="store_true", help="write it (default: show what would be written)")
    rd = sub.add_parser("read")
    rd.add_argument("log_id", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "login":
        padi.login(a.email)
    elif a.cmd == "edit":
        from .editfile import edit

        answers = Answers(interactive=False)
        rows = prepare(a, answers)  # suggestions (weather, footage) without asking anything
        edit(rows, Answers(interactive=False))
    elif a.cmd == "training":
        log_training(a)
    elif a.cmd == "read":
        with padi.browser() as ctx:
            page = ctx.new_page()
            if not padi.open_existing(page, a.log_id):
                sys.exit("could not open that entry (signed in?)")
            for k, v in padi.read_form(page).items():
                print(f"{k}: {v}")
    else:
        run(a, write=a.cmd == "apply")


if __name__ == "__main__":
    main()
