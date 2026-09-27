"""Drive the PADI logbook web page (learning.padi.com) with Playwright, as the signed-in diver.

Deliberately uses only the website's own forms, never its backend API directly.
Fields are found by the question text the page shows, so the code reads like the form."""

import os
import re
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from .store import HOME

BASE = "https://learning.padi.com"
SESSION = HOME / "session.json"
ENV_FILE = Path(os.environ.get("PADI_ENV", HOME / ".env"))

# Choice groups (tiles) by their heading, and free inputs by their card title.
CHOICES = [
    "Type of Dive",
    "Water Type",
    "Body of Water",
    "Weather",
    "Visibility",
    "Waves",
    "Current",
    "Surge",
    "Suit",
    "Cylinder",
    "Gas Mixture",
    "Feeling",
]
INPUTS = {
    "Dive Title": "Dive Title",
    "Max Depth": "Max Depth",
    "Bottom Time": "Bottom Time",
    "Air Temperature": "Air Temperature",
    "Surface Temperature": "Surface Temperature",
    "Bottom Temperature": "Bottom Temperature",
    "How far could you see?": "How far could you see?",
    "Weight": "Weight",
    "Cylinder size": "What was the cylinder size?",
    "Starting Pressure": "Starting Pressure",
    "Ending Pressure": "Ending Pressure",
    "Buddy": "Buddy",
    "Dive Center": "Dive Center",
}
MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def goto(page: Page, url: str):
    """Navigate without waiting for the full load event: some third-party scripts on PADI's
    pages never finish loading in a visible browser, so 'load' can hang forever."""
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)


def read_env(path: Path | None = None) -> dict:
    """PADI_EMAIL and PADI_PW: real environment variables win over the env file."""
    path = path or ENV_FILE
    env = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k in ("PADI_EMAIL", "PADI_PW") and v})
    return env


@contextmanager
def browser(headless=True):
    with sync_playwright() as p:
        b = p.chromium.launch(headless=headless)
        ctx = b.new_context(
            storage_state=str(SESSION) if SESSION.exists() else None, viewport={"width": 1100, "height": 1400}
        )
        try:
            yield ctx
        finally:
            if SESSION.exists() or not headless:
                ctx.storage_state(path=str(SESSION))
            b.close()


TOKEN_JS = """Object.keys(localStorage).some(
                 k => k.startsWith('CognitoIdentityServiceProvider.') && k.endsWith('.idToken'))
             || !!localStorage.getItem('accessWithIdToken')"""


def _has_token(page: Page) -> bool:
    """Signed in once the site has stored its Cognito id token (or shows the logbook)."""
    try:
        if "learning.padi.com" not in page.url:
            return False
        return bool(page.evaluate(TOKEN_JS)) or page.get_by_text("Total Dives").count() > 0
    except Exception:
        return False  # mid-navigation


def login(email: str | None = None):
    """Sign in with PADI_EMAIL / PADI_PW (environment or env file). Saves the browser session."""
    env = read_env()
    email, pw = email or env.get("PADI_EMAIL"), env.get("PADI_PW")
    if not email or not pw:
        raise SystemExit(f"set PADI_EMAIL and PADI_PW in the environment or in {ENV_FILE} (see README)")
    HOME.mkdir(parents=True, exist_ok=True)
    print("A browser window will open. Don't close it; it closes itself when signed in.")
    print("If PADI asks for a code or a captcha, complete it in that window (3 minutes).")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False)
        ctx = b.new_context()
        page = ctx.new_page()
        try:
            step = "open the sign-in page"
            goto(page, f"{BASE}/logbook")  # the site sends signed-out visitors to its own sign-in page
            email_box = page.locator("input[placeholder=Email]:visible")
            email_box.wait_for(timeout=60_000)
            step = "enter the email"
            email_box.click()
            email_box.press_sequentially(email, delay=30)
            email_box.press("Enter")
            step = "reach the password step"
            pw_box = page.locator("input[type=password]:visible")  # a hidden copy exists on the email step
            pw_box.wait_for(timeout=30_000)
            step = "enter the password"
            pw_box.click()
            page.keyboard.insert_text(pw)
            step = "submit"
            pw_box.press("Enter")
            # Wait for the site to finish its own sign-in (it stores a token), rather than navigating away early.
            for _ in range(180):
                if _has_token(page):
                    break
                page.wait_for_timeout(1000)
            else:
                raise SystemExit("sign-in did not complete within 3 minutes")
            ctx.storage_state(path=str(SESSION))
            os.chmod(SESSION, 0o600)
            ok = signed_in(page)
        except SystemExit:
            raise
        except Exception as e:
            # Never re-raise Playwright errors here: their call logs can contain the typed password.
            if "closed" in str(e).lower():
                raise SystemExit(
                    "the browser window was closed before sign-in finished; run `padi-sync login` again"
                ) from None
            raise SystemExit(
                f"sign-in failed at step: {step} ({type(e).__name__}). Run `padi-sync login` again."
            ) from None
        finally:
            try:
                b.close()
            except Exception:
                pass
    print(
        f"signed in; session saved to {SESSION}"
        + ("" if ok else " (but the logbook page didn't load; try `padi-sync plan`)")
    )


def signed_in(page: Page) -> bool:
    """True once the logbook page shows its buttons. PADI's logbook is sometimes slow to load its
    data, so wait generously and reload once before deciding the session is gone."""
    for _ in range(2):
        goto(page, f"{BASE}/logbook")
        for _ in range(60):
            if "account.padi.com" in page.url:
                return False
            if page.get_by_text("Log recreational dive").count() and page.get_by_text("Total Dives").count():
                return True
            page.wait_for_timeout(1000)
    # Buttons but no list: signed in, but the logbook data didn't load.
    return page.get_by_text("Log recreational dive").count() > 0


def _open_sections(page: Page):
    for t in page.locator(".collapse-item__title").all():
        classes = (t.locator("xpath=..").get_attribute("class") or "").split()
        if "active" not in classes:  # an expanded section is marked "active"
            t.click()
    page.wait_for_timeout(300)


def _card(page: Page, title: str):
    # A card whose title or description is exactly `title`, then its single input or textarea.
    return page.locator(".card").filter(has=page.locator(".title, .description").get_by_text(title, exact=True)).first


def _choices(page: Page, heading: str):
    h = page.locator("h6.heading, .card .title").get_by_text(heading, exact=True).first
    return h.locator("xpath=following::div[contains(@class,'card-choices')][1]")


def tile_label(tile) -> str:
    """The option's visible label. Its checkbox value is an internal code that differs from the
    label for some groups ('Light' current is 'SomeCurrent', EANx32 is 'Enriched_32')."""
    return " ".join(tile.locator(".text").inner_text().split())


def read_form(page: Page) -> dict:
    _open_sections(page)
    out = {}
    for key, title in INPUTS.items():
        inp = _card(page, title).locator("input.input-card")
        if inp.count():
            out[key] = inp.first.input_value()
    for heading in CHOICES:
        box = _choices(page, heading)
        if box.count():
            picked = [
                tile_label(t)
                for t in box.locator(".card-choice").all()
                if t.locator("input[type=checkbox]").is_checked()
            ]
            if picked:
                out[heading] = picked[0]
    instr = page.locator("input[placeholder='PADI Number']")
    if instr.count() and instr.first.input_value():
        out["Instructor"] = instr.first.input_value()
    site = page.locator(".card.search input.search")
    if site.count():
        out["Dive Site"] = ((site.first.get_attribute("selected") or "") or site.first.input_value()).strip()
    note = page.locator("textarea")
    if note.count():
        out["Note"] = note.first.input_value()
    date = page.locator(".date-content")
    if date.count():
        day = date.locator("input[placeholder=Day]").input_value()
        month = date.locator("select").input_value()
        year = date.locator("input[placeholder=Year]").input_value()
        if day and month and year:
            out["Date"] = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return out


def same(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 0.05
    except (TypeError, ValueError):
        return str(a or "").strip() == str(b or "").strip()


def diff(current: dict, wanted: dict) -> dict:
    """Fields to change. Never blanks a field the diver filled in by hand."""
    return {k: (current.get(k), v) for k, v in wanted.items() if not same(current.get(k), v)}


def fill(page: Page, changes: dict):
    _open_sections(page)
    for key, (_, value) in changes.items():
        if key == "Date":
            y, m, d = value.split("-")
            box = page.locator(".date-content")
            box.locator("input[placeholder=Day]").fill(str(int(d)))
            box.locator(".select__ui").click()
            page.locator(".select__ui, .select__options, [role=listbox]").get_by_text(
                MONTHS[int(m) - 1], exact=True
            ).last.click()
            box.locator("input[placeholder=Year]").fill(y)
        elif key == "Instructor":
            page.locator("input[placeholder='PADI Number']").first.fill(str(value))
        elif key == "Dive Site":
            pick_site(page, str(value))
        elif key == "Note":
            page.locator("textarea").first.fill(str(value))
        elif key in INPUTS:
            _card(page, INPUTS[key]).locator("input.input-card").first.fill(str(value))
        elif key in CHOICES:
            box = _choices(page, key)
            tiles = box.locator(".card-choice").all()
            if not any(tile_label(t) == value for t in tiles):
                raise KeyError(f"{key}: no option labelled {value!r}")
            for tile in tiles:
                if (tile_label(tile) == value) != tile.locator("input[type=checkbox]").is_checked():
                    tile.click()
        else:
            raise KeyError(f"no form field for {key}")


def site_suggestions(page: Page, query: str) -> list[str]:
    """PADI's own dive site names matching `query` (the form's search box)."""
    box = page.locator(".card.search input.search").first
    box.fill("")
    box.press_sequentially(query, delay=40)
    try:
        page.locator(".card.search .result").first.wait_for(timeout=8_000)
    except Exception:
        return []
    return [" ".join(t.split()) for t in page.locator(".card.search .result").all_inner_texts()]


def pick_site(page: Page, name: str):
    """Choose `name` from PADI's suggestions; if it isn't one, leave it as typed text."""
    for i, text in enumerate(site_suggestions(page, name)):
        if text.lower() == name.lower():
            page.locator(".card.search .result").nth(i).click()
            return
    page.keyboard.press("Tab")


def open_new(page: Page):
    goto(page, f"{BASE}/logbook/log-dive/recreational/create")
    page.wait_for_selector("text=Dive Title", state="attached", timeout=90_000)


def open_existing(page: Page, log_id: int) -> bool:
    goto(page, f"{BASE}/logbook/log-dive/recreational/update/{log_id}")
    try:
        page.wait_for_selector("text=Dive Title", state="attached", timeout=90_000)
        page.wait_for_timeout(1500)  # let the form fill in its saved values
        return True
    except Exception:
        return False


def submit(page: Page, new: bool):
    page.get_by_role("button", name="Log Dive" if new else "Update").click()
    page.wait_for_url(re.compile(r"/logbook(\?|$|/(?!log-dive))"), timeout=30_000)


NEXT_PAGE = ".pagination .base-element:has(.icon_arrow-right)"


def list_logs(page: Page) -> list[dict]:
    """Every logbook entry: id, kind (training/recreational), date text, title, location, status."""
    if not signed_in(page):
        raise RuntimeError("logbook didn't load")
    page.locator(".list-content a[href*='/logbook/log-dive/']").first.wait_for(timeout=60_000)
    rows = []
    for _ in range(100):
        for a in page.locator(".list-content a[href*='/logbook/log-dive/']").all():
            href = a.get_attribute("href")
            m = re.search(r"/log-dive/(\w+)/update/(\d+)", href or "")
            if not m:
                continue
            card = a.locator(".card-logbook")
            rows.append(
                {
                    "id": int(m.group(2)),
                    "kind": m.group(1),
                    "date": card.locator(".date").inner_text().strip(),
                    "title": card.locator(".title").inner_text().strip(),
                    "location": card.locator(".location").inner_text().strip(),
                    "status": card.locator(".log-status").inner_text().strip(),
                }
            )
        nxt = page.locator(NEXT_PAGE).first
        if not nxt.count() or "element--disabled" in (nxt.get_attribute("class") or ""):
            break
        before = page.locator("#summary").inner_text()  # "Showing 1-15 of 27"
        nxt.click()
        page.wait_for_function("t => document.querySelector('#summary')?.innerText !== t", arg=before, timeout=20_000)
        page.locator(".list-content a[href*='/logbook/log-dive/']").first.wait_for(timeout=20_000)
    return list({r["id"]: r for r in rows}.values())


def find_id(page: Page, title: str, date_text: str) -> int | None:
    hits = [r["id"] for r in list_logs(page) if r["title"] == title and r["date"] == date_text]
    return max(hits) if hits else None


NO_FLEXIBLE = "No flexible skills completed on this dive"


def open_training(page: Page, course: str, dive_label: str):
    """Pick the course and training dive number, as the 'Log training dive' button does."""
    goto(page, f"{BASE}/logbook/log-dive/select-course")
    page.locator(".select-course .select__toggle").first.wait_for(timeout=60_000)
    page.locator(".select-course .select__toggle").first.click()
    page.locator(".select-course").get_by_text(course, exact=True).click()
    page.locator(".select-course").get_by_text(dive_label, exact=True).click()
    page.wait_for_selector("text=Dive Title", state="attached", timeout=90_000)
    page.wait_for_timeout(1500)
    _open_sections(page)


def tick_no_flexible(page: Page):
    """A plain labelled checkbox (not a tile) in the Flexible Skills list."""
    box = page.locator(".flexible__skills input[type=checkbox][value='no skills']")
    if not box.is_checked():
        page.locator(".flexible__skills label[for='no skills']").click()
    if not box.is_checked():
        raise RuntimeError("could not tick 'No flexible skills completed on this dive'")
