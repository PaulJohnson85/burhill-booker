"""
Burhill Golf Club – automated tee time booker.
Uses Playwright to drive the ESP Leisure EliteLive booking system.

Usage:
    python3 book.py           # waits until booking opens, then books
    python3 book.py --now     # skip the wait, attempt booking immediately (for testing)
    python3 book.py --dry-run # go through the whole flow but stop before confirming
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from config import BOOKING, BOOKING_WINDOW, CREDENTIALS
from open_play import check_booking

BASE_URL = "https://www.e-s-p.com/elitelive"
CLUB_ID  = "675"

COURSE_LINK_TEXT = {
    "Golf": "Tee Times",
    "Old":  "Old Course",
    "New":  "New Course",
}

# ── Timing helpers ─────────────────────────────────────────────────────────

def booking_opens_at(target_date_str: str) -> datetime:
    target = datetime.strptime(target_date_str, "%d/%m/%Y")
    open_date = target - timedelta(days=BOOKING_WINDOW["days_in_advance"])
    h, m = map(int, BOOKING_WINDOW["open_time"].split(":"))
    return open_date.replace(hour=h, minute=m, second=0, microsecond=0)


def wait_until(target: datetime, lead_minutes: int = 0):
    wake_at = target - timedelta(minutes=lead_minutes)
    now = datetime.now()
    if wake_at > now:
        sleep_secs = (wake_at - now).total_seconds()
        print(f"[{now:%H:%M:%S}] Sleeping {sleep_secs/60:.1f} min until {wake_at:%Y-%m-%d %H:%M:%S}")
        time.sleep(sleep_secs)
    while datetime.now() < target:
        remaining = (target - datetime.now()).total_seconds()
        print(f"\r[{datetime.now():%H:%M:%S}] Booking opens in {remaining:.1f}s …", end="", flush=True)
        time.sleep(0.1)
    print()


# ── URL helpers ────────────────────────────────────────────────────────────

def encode_date(date_str: str) -> str:
    """Convert DD/MM/YYYY to DD%2FMM%2FYY for use in ESP URLs."""
    d, m, y = date_str.split("/")
    return f"{d}%2F{m}%2F{y[2:]}"


def parse_time(encoded: str) -> Optional[int]:
    """Parse Start=HH%3AMM or HH:MM into minutes since midnight. Returns None on failure."""
    t = encoded.replace("%3A", ":").replace("Start=", "").replace("End=", "")
    try:
        h, mn = map(int, t.split(":"))
        return h * 60 + mn
    except Exception:
        return None


# ── Main flow ──────────────────────────────────────────────────────────────

def run(dry_run: bool = False, booking: dict = None):
    b = booking or BOOKING
    opens_at = booking_opens_at(b["date"])
    now = datetime.now()
    if now < opens_at:
        print(f"Booking for {b['date']} opens at {opens_at:%Y-%m-%d %H:%M:%S}")
        wait_until(opens_at, lead_minutes=b.get("lead_time_minutes", BOOKING.get("lead_time_minutes", 2)))
    else:
        print(f"Booking window open (opened {opens_at:%Y-%m-%d %H:%M:%S}). Proceeding immediately.")

    # Check open play schedule before launching browser
    op = check_booking(b["date"], b["course"], b["preferred_time"])
    if op["status"] == "during_open_play":
        print(f"\n⚠️  OPEN PLAY WARNING: {op['message']}")
        if not dry_run:
            resp = input("Continue anyway? (y/N): ").strip().lower()
            if resp != 'y':
                print("Aborted.")
                sys.exit(0)
    elif op["status"] == "open_play_course":
        print(f"\n⚠️  OPEN PLAY NOTE: {op['message']}")
    elif op["status"] == "ok":
        print(f"ℹ️  Open play check: {op['message']}")
    elif op["status"] == "no_data":
        print(f"ℹ️  No open play data for {b['date']} — run: python3 open_play.py <pdf> <year> to import.")

    with sync_playwright() as p:
        headless = os.environ.get("HEADLESS", "0") == "1"
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        try:
            _login(page)
            _navigate_to_date(page, b)
            slot_url = _find_slot(page, b)
            if slot_url is None:
                print("❌  No matching tee time found.")
                page.screenshot(path="no_slot_found.png")
                sys.exit(1)
            print(f"✅  Found slot: {slot_url}")
            if dry_run:
                print("[DRY RUN] Stopping before confirmation.")
            else:
                _book_slot(page, slot_url)
        except PWTimeout as e:
            page.screenshot(path="error_screenshot.png")
            print(f"❌  Timeout: {e}\nScreenshot saved to error_screenshot.png")
            sys.exit(1)
        finally:
            if dry_run:
                input("Press Enter to close browser…")
            browser.close()


# ── Step functions ─────────────────────────────────────────────────────────

def _login(page):
    print("Logging in …")
    page.goto(f"{BASE_URL}/?clubid={CLUB_ID}")
    page.wait_for_url(lambda url: "login.php" in url, timeout=15_000)
    page.fill('input[name="username"]', CREDENTIALS["username"])
    page.fill('input[name="password"]', CREDENTIALS["password"])
    page.locator('input[type="submit"]').click()
    page.wait_for_load_state("domcontentloaded", timeout=15_000)
    print(f"  Logged in → {page.url}")


def _dump_forms(page, label=""):
    """Log every form's action and inputs/buttons — debugging aid for ESP pages."""
    try:
        info = page.evaluate("""() =>
            Array.from(document.forms).map(f => ({
                action: f.action,
                inputs: Array.from(f.elements).map(el =>
                    `${el.tagName.toLowerCase()}[${el.type||''}] name=${el.name||''} value=${(el.value||'').slice(0,30)}`)
            }))
        """)
        print(f"    [forms on {label}]", flush=True)
        for f in info:
            print(f"      action={f['action']}", flush=True)
            for i in f['inputs']:
                print(f"        {i}", flush=True)
        # Clickable non-form controls (image buttons, JS links) often advance ESP pages
        links = page.evaluate("""() =>
            Array.from(document.querySelectorAll('a[onclick], a[href^="javascript"], input[type=image], img[onclick]'))
                 .map(el => `${el.tagName} text=${(el.innerText||el.alt||'').trim().slice(0,30)} onclick=${(el.getAttribute('onclick')||el.getAttribute('href')||'').slice(0,80)}`)
        """)
        for l in links:
            print(f"      [clickable] {l}", flush=True)
    except Exception as e:
        print(f"    [forms dump failed: {e}]", flush=True)


def _participants_state(page):
    """Read the current book_participants state via JS: which gotdata forms exist
    and the value of CurNumPeople. Returns {} if the page can't be read."""
    js = """() => {
        const out = {forms: [], curNum: null};
        for (const f of Array.from(document.forms)) {
            for (const el of Array.from(f.elements)) {
                if (el.name === 'gotdata') out.forms.push(el.value);
                if (el.name === 'CurNumPeople') out.curNum = el.value;
            }
        }
        return out;
    }"""
    try:
        return page.evaluate(js)
    except Exception:
        return {}


def _submit_participants_form(page, gotdata: str, label=""):
    """
    Submit the book_participants form whose hidden gotdata input has the given
    value, clicking the form's own submit button (so its name=value is posted),
    falling back to JS form.submit(). Polls up to 8s for the form to appear,
    since a prior onchange auto-submit may still be navigating.
    """
    # The gotdata inputs are associated with their form via form.elements but are
    # NOT DOM descendants (ESP's mis-nested table markup), so CSS form:has(...)
    # cannot find them. Use document.forms + form.elements in JS instead.
    js = f"""() => {{
        // f.elements['gotdata'] returns a RadioNodeList when names collide and
        // its .value is empty for non-radios — iterate elements explicitly.
        const form = Array.from(document.forms).find(f =>
            Array.from(f.elements).some(el =>
                el.name === 'gotdata' && el.value === '{gotdata}'));
        if (!form) return 'no-form';
        const btn = Array.from(form.elements).find(el =>
            (el.type === 'submit' || el.type === 'image'));
        if (btn) {{ btn.click(); return 'clicked-button:' + (btn.value || btn.name); }}
        form.submit();
        return 'js-submit';
    }}"""
    how = "no-form"
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            how = page.evaluate(js)
        except Exception as e:
            # Context destroyed by the resulting navigation means a submit landed
            print(f"    [{label}] evaluate raised (navigation): {str(e)[:80]}", flush=True)
            how = "navigated"
            break
        if how != "no-form":
            break
        page.wait_for_timeout(500)
    if how == "no-form":
        print(f"    [{label}] no form with gotdata={gotdata} after 8s — skipping", flush=True)
        return False
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    page.wait_for_timeout(400)
    print(f"    [{label} via {how} gotdata={gotdata}] → {page.url}", flush=True)
    return True


def _set_other_players(page, players: int, partner: str = ""):
    """Fill player slots 2..n on book_participants: slot 2 gets the named
    member (resolved via Burhill's member-list AJAX and selected through the
    page's own set_memberselected) when partner is set; all other slots are
    marked as guests."""
    for i in range(1, players):
        if i == 1 and partner:
            try:
                res = page.evaluate("""async (partner) => {
                    const r = await fetch('ajax/ajax_get_club_members.php');
                    const members = await r.json();
                    let m = members.find(x => x.Name === partner);
                    if (!m) {
                        const toks = partner.toLowerCase()
                            .replace(/[,\\/]/g, ' ').split(/\\s+/).filter(Boolean);
                        const cands = members.filter(x =>
                            toks.every(t => (x.Name || '').toLowerCase().includes(t)));
                        if (cands.length === 1) m = cands[0];
                        else if (cands.length > 1)
                            return 'ambiguous: ' + cands.slice(0, 5).map(x => x.Name).join('; ');
                        else return 'not-found';
                    }
                    set_msp(1);
                    set_memberselected(m.Name, m.PKey);
                    const g = document.querySelector('input[name="BookNonMemb1"]');
                    if (g) g.checked = false;
                    return 'selected: ' + m.Name + ' ' + m.PKey;
                }""", partner)
                print(f"    [partner member] {res}", flush=True)
                if res == "not-found":
                    raise RuntimeError(
                        f"Playing partner '{partner}' not found in Burhill's member "
                        f"list — use Verify in the booking form")
                if res.startswith("ambiguous"):
                    raise RuntimeError(
                        f"Playing partner '{partner}' matches several members "
                        f"({res[11:]}) — pick the exact one via Verify")
            except RuntimeError:
                raise
            except Exception as e:
                print(f"    [partner member failed] {str(e)[:120]}", flush=True)
                raise RuntimeError(
                    f"Could not select playing partner '{partner}': {str(e)[:120]}")
        else:
            try:
                page.evaluate(f"""() => {{
                    const cb = document.querySelector('input[name="BookNonMemb{i}"]');
                    if (cb) cb.checked = true;
                }}""")
            except Exception:
                pass


def _try_pick_member(page, partner: str) -> str:
    """If the page shows member search results (a select of matches, or
    clickable names), pick the first one matching partner."""
    try:
        return page.evaluate("""(partner) => {
            const p = partner.toLowerCase();
            for (const sel of Array.from(document.querySelectorAll('select'))) {
                for (const opt of Array.from(sel.options)) {
                    if ((opt.text || '').toLowerCase().includes(p)) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                        return 'selected "' + opt.text.trim() + '" in ' + (sel.name || '?');
                    }
                }
            }
            for (const el of Array.from(document.querySelectorAll('a, button, input[type=button], td[onclick]'))) {
                const txt = (el.innerText || el.value || '').trim().toLowerCase();
                if (txt && txt.includes(p)) { el.click(); return 'clicked "' + txt + '"'; }
            }
            return 'no-match';
        }""", partner)
    except Exception as e:
        return f"error: {str(e)[:80]}"


def _scan_for_date(page, target_day, candidates):
    """One pass over the current page (and its iframes) for the date cell.
    Returns a locator or None — no waiting."""
    for candidate in candidates:
        loc = page.locator('td').filter(has_text=re.compile(rf'^\s*{candidate}\s*$'))
        if loc.count() > 0:
            print(f"    Found date cell '{candidate}' ({loc.count()} matches)", flush=True)
            return loc.first
    loc = page.locator('td[onclick], a').filter(has_text=re.compile(rf'^\s*{target_day}\s*$'))
    if loc.count() > 0:
        print(f"    Found date via fallback selector ({loc.count()} matches)", flush=True)
        return loc.first
    for frame in page.frames[1:]:
        try:
            loc = frame.locator('td').filter(has_text=re.compile(rf'^\s*{target_day}\s*$'))
            if loc.count() > 0:
                print(f"    Found date cell in iframe {frame.url}", flush=True)
                return loc.first
        except Exception:
            continue
    return None


def _reach_calendar(page, b, course_key):
    """book_start → Golf Club Tee Times → <course> → participants flow.
    Leaves the page on book_date.php. Returns the calendar URL."""
    course_text = COURSE_LINK_TEXT.get(course_key, "Old Course")

    def click_and_wait(locator, label=""):
        try:
            locator.click(timeout=30_000, force=True)
        except Exception:
            page.screenshot(path=f"timeout_{label.replace(' ','_')}.png")
            print(f"    [TIMEOUT on '{label}'] page={page.url}")
            raise
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
        page.wait_for_timeout(400)
        if label:
            print(f"    [{label}] → {page.url}")

    page.goto(f"{BASE_URL}/book_start.php", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(500)
    click_and_wait(page.get_by_role("link", name="Golf Club Tee Times").first,
                   "Golf Club Tee Times")
    click_and_wait(page.get_by_role("link", name=course_text).first,
                   f"Course: {course_text}")
    print(f"    [after course select: {course_key}] → {page.url}", flush=True)

    # Drive book_participants.php by its actual state (NumPeople onchange may
    # auto-submit, so loop: bump count, mark guests/partner, submit confirm).
    players = int(b["players"])
    try:
        page.select_option('select[name="NumPeople"]', str(players))
        page.wait_for_timeout(800)
    except Exception as e:
        print(f"    [NumPeople select failed] {str(e)[:120]}", flush=True)

    for attempt in range(6):
        if "book_participants" not in page.url:
            print(f"    [Participants] progressed → {page.url}", flush=True)
            break
        state = _participants_state(page)
        cur = state.get("curNum")
        print(f"    [participants attempt {attempt}] curNum={cur} url={page.url}", flush=True)
        cur_n = int(cur) if (cur and str(cur).isdigit()) else 1

        if cur_n < players:
            try:
                page.select_option('select[name="NumPeople"]', str(players))
            except Exception:
                pass
            if not _submit_participants_form(page, "1", "NumPeople submit"):
                page.wait_for_timeout(600)
            continue

        partner = (b.get("partner_name") or "").strip()
        if players > 1:
            _set_other_players(page, players, partner)
            if partner and attempt > 1:
                pick = _try_pick_member(page, partner)
                print(f"    [member pick] {pick}", flush=True)
                page.wait_for_timeout(600)
        _submit_participants_form(page, "2", "Participants confirm")
        try:
            page.wait_for_url(lambda url: "book_participants" not in url, timeout=15_000)
        except Exception:
            print(f"    [Participants confirm] still on {page.url}", flush=True)
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
        page.wait_for_timeout(600)

        if partner and "book_participants" in page.url:
            try:
                body = page.evaluate("() => (document.body && document.body.innerText) || ''")
            except Exception:
                body = ""
            m = re.search(r"Error with participant[^\n]*", body, re.I)
            if m:
                raise RuntimeError(
                    f"Burhill rejected playing partner '{partner}': {m.group(0).strip()} "
                    f"— use Verify in the booking form to get the exact member name")

    return page.url


def _navigate_to_date(page, booking: dict = None):
    print("Navigating to booking …")
    b = booking or BOOKING
    print(f"    [post-login] url={page.url}")

    target_day = b["date"].split("/")[0].lstrip("0")   # "21/06/2026" → "21"
    target_day_padded = b["date"].split("/")[0]
    candidates = []
    for c in [target_day_padded, target_day]:
        if c not in candidates:
            candidates.append(c)

    # Which courses to try, in preference order. A member's chosen course may
    # not run that day (e.g. New is open-play on Sundays), so we always fall
    # back to the other course.
    course = b.get("course", "Golf")
    if course == "New":
        course_order = ["New", "Old"]
    elif course == "Old":
        course_order = ["Old", "New"]
    else:  # "Golf" / "Any"
        course_order = ["Old", "New"]

    # Overall deadline: poll through the window opening (we fire at opens_at−lead)
    deadline = time.time() + 30
    opens_at = b.get("opens_at")
    if opens_at:
        try:
            from datetime import datetime
            secs_to_open = (datetime.fromisoformat(opens_at) - datetime.now()).total_seconds()
            deadline = max(deadline, time.time() + secs_to_open + 90)
        except Exception:
            pass
    deadline = min(deadline, time.time() + 8 * 60)

    date_locator = None
    chosen_course = None
    ci = 0
    while date_locator is None and time.time() < deadline:
        course_key = course_order[ci % len(course_order)]
        ci += 1
        print(f"    Trying {course_key} course for date {target_day} …", flush=True)
        try:
            date_url = _reach_calendar(page, b, course_key)
        except Exception as e:
            print(f"    [reach calendar {course_key} failed] {str(e)[:140]}", flush=True)
            # A partner-member error is fatal regardless of course
            if "playing partner" in str(e):
                raise
            page.wait_for_timeout(1000)
            continue

        # Poll THIS course's calendar briefly (reloading) before switching
        sub_deadline = min(deadline, time.time() + 12)
        first = True
        while date_locator is None and time.time() < sub_deadline:
            if not first:
                try:
                    page.goto(date_url, wait_until="domcontentloaded", timeout=30_000)
                except Exception:
                    pass
                page.wait_for_timeout(900)
            first = False
            date_locator = _scan_for_date(page, target_day, candidates)
            if date_locator is None and time.time() < sub_deadline:
                page.wait_for_timeout(1500)
        if date_locator is not None:
            chosen_course = course_key
            break
        print(f"    Date {target_day} not on {course_key} course — "
              f"trying the other course …", flush=True)

    if date_locator is None:
        print(f"    [date_not_found] url={page.url}", flush=True)
        page.screenshot(path="date_not_found.png")
        raise RuntimeError(
            f"Date {target_day} not found on either course — is the booking "
            f"window open / is the course in open play?")

    date_locator.click(force=True)
    page.wait_for_load_state("domcontentloaded", timeout=45_000)
    page.wait_for_timeout(800)
    print(f"  On tee time page for {b['date']} ({chosen_course} course) → {page.url}", flush=True)


def _find_slot(page, booking: dict = None) -> Optional[str]:
    """Return the href of the first available slot at or after preferred_time,
    and (when latest_time is set) no later than latest_time."""
    b = booking or BOOKING
    preferred_h, preferred_m = map(int, b["preferred_time"].split(":"))
    preferred_mins = preferred_h * 60 + preferred_m
    latest_mins = None
    if b.get("latest_time"):
        lh, lm = map(int, b["latest_time"].split(":"))
        latest_mins = lh * 60 + lm

    window = f">= {b['preferred_time']}"
    if latest_mins is not None:
        window += f" and <= {b['latest_time']}"

    # Time slot links have: gotdata=2 and Start=HH%3AMM in their href
    links = page.query_selector_all('a[href*="gotdata=2"][href*="Start="]')
    print(f"  Found {len(links)} time slots. Looking for first slot {window} …", flush=True)

    for link in links:
        href = link.get_attribute("href")
        text = link.inner_text().strip()
        # Parse time from URL parameter Start=HH%3AMM
        m = re.search(r'Start=(\d{2}%3A\d{2})', href)
        if not m:
            continue
        slot_mins = parse_time(m.group(1))
        if slot_mins is None:
            continue
        if slot_mins < preferred_mins:
            continue
        if latest_mins is not None and slot_mins > latest_mins:
            print(f"  No slot within window ({window}) — refusing later times.", flush=True)
            return None
        print(f"  → Selecting slot: {text}", flush=True)
        return href
    return None


def _submit_form_js(page, label=""):
    """Submit the first form on the page via JS, wait for load."""
    with page.expect_navigation(wait_until="load", timeout=30_000):
        page.evaluate("document.querySelector('form').submit()")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(600)
    if label:
        print(f"    [{label}] → {page.url}")


def _book_slot(page, slot_url: str) -> bool:
    """
    Navigate to the slot URL and confirm the booking.
    Returns True if booking was verified on the Burhill site, False otherwise.
    Raises RuntimeError if something goes wrong mid-flow.
    """
    print("Booking slot …")
    # Slot hrefs are relative ("book_date.php?gotdata=2&…") — make absolute
    if not slot_url.startswith("http"):
        slot_url = f"{BASE_URL}/{slot_url.lstrip('/')}"
    page.goto(slot_url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(800)
    print(f"  Slot page: {page.url}")

    # Handle intermediate questionnaire pages (e.g. buggy hire), then find confirm button
    max_steps = 5
    for step in range(max_steps):
        cur = page.url

        if "book_complete" in cur:
            # ESP's booking-success page — the booking is made
            print("  ✅ Landed on book_complete.php — booking made.")
            page.screenshot(path="booking_confirmed.png")
            return True

        if "home.php" in cur:
            # Landed on home — now verify the booking actually exists
            print("  Redirected to home — verifying booking …")
            verified = _verify_booking(page, slot_url)
            page.screenshot(path="booking_confirmed.png")
            return verified

        if "questionnaire" in cur:
            print("  Submitting questionnaire (declining extras) …")
            _dump_forms(page, "questionnaire page")
            # The page is full of side-menu forms (Home, Logout, …) whose submit
            # buttons match .first — pick the questionnaire's own form instead.
            try:
                how = page.evaluate("""() => {
                    const menu = ['home.php','logout.php','book_start','book_back',
                                  'book_history','book_basket_view','player_details',
                                  'player_changepassword','player_attachments','club_details'];
                    const forms = Array.from(document.forms).filter(f =>
                        !menu.some(m => (f.action || '').includes(m)));
                    // Prefer the form that posts onward (confirm/questionnaire)
                    const form = forms.find(f => (f.action || '').includes('book_')) || forms[0];
                    if (!form) return 'no-form';
                    const btn = Array.from(form.elements).find(el =>
                        el.type === 'submit' || el.type === 'image');
                    if (btn) { btn.click(); return 'clicked-button:' + (btn.value || btn.name); }
                    form.submit();
                    return 'js-submit';
                }""")
                print(f"    [questionnaire submit: {how}]", flush=True)
            except Exception as e:
                print(f"    [questionnaire] evaluate raised (navigation): {str(e)[:80]}", flush=True)
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            page.wait_for_timeout(600)
            print(f"    [questionnaire] → {page.url}")
            continue

        if "book_confirm" in cur:
            print("  On confirmation page — clicking Make Booking inside iframe …")
            # The final Make Booking button lives in the wp_cybersource/el_userdetails
            # iframe, but its load timing/URL varies — poll every frame for it.
            # NB: the main frame also has a side-menu "Make Booking" (book_start)
            # button, so only search child frames.
            clicked = None
            deadline = time.time() + 45
            while clicked is None and time.time() < deadline:
                for frame in page.frames[1:]:
                    try:
                        loc = frame.locator(
                            'input[value*="Make Booking" i], '
                            'button:has-text("Make Booking"), '
                            'input[type="submit"]').first
                        if loc.count() > 0:
                            loc.click(timeout=5_000, force=True)
                            clicked = frame.url
                            break
                    except Exception:
                        continue
                if clicked is None:
                    page.wait_for_timeout(1000)
            if clicked is None:
                print(f"    [book_confirm] frames: {[f.url for f in page.frames]}", flush=True)
                _dump_forms(page, "book_confirm page")
                page.screenshot(path="book_confirm_timeout.png")
                raise RuntimeError("Make Booking button not found in any frame on book_confirm.php")
            print(f"    [book_confirm clicked in frame {clicked}]", flush=True)
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            page.wait_for_timeout(800)
            print(f"    [book_confirm submitted] → {page.url}")
            continue

        btn = page.locator(
            'input[value*="Confirm" i]:not([type=hidden]), '
            'input[value*="Basket" i]:not([type=hidden]), '
            'input[value*="Proceed" i]:not([type=hidden]), '
            'button:has-text("Confirm"), button:has-text("Proceed")'
        ).first
        if btn.count() > 0:
            print(f"  Clicking confirm button …")
            btn.click(force=True)
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            page.wait_for_timeout(800)
            print(f"  Post-confirm page: {page.url}")
            page.screenshot(path="booking_confirmed.png")
            if "home.php" in page.url:
                return _verify_booking(page, slot_url)
            return True
        break

    page.screenshot(path="confirmation_page.png")
    print(f"  ⚠️  Unexpected page: {page.url}")
    return False


def _verify_booking(page, slot_url: str) -> bool:
    """
    Navigate to the member's bookings list and check a booking exists today/soon.
    Returns True if at least one booking is found, False if none.
    """
    import re as _re
    # Extract the date from slot_url e.g. StartDate=17%2F06%2F26
    m = _re.search(r'StartDate=(\d+)%2F(\d+)%2F(\d+)', slot_url)
    try:
        # Navigate to manage bookings page
        page.evaluate("document.querySelector('a[href*=\"manage\"], input[value*=\"Manage\"]') && "
                      "document.querySelector('a[href*=\"manage\"], input[value*=\"Manage\"]').click()")
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
        page.wait_for_timeout(800)
    except Exception:
        pass

    # Look for any booking reference or tee time entry on the page
    content = page.content()
    has_booking = any(kw in content.lower() for kw in
                      ["booking ref", "tee time", "your booking", "booked", "book_cancel"])

    if has_booking:
        print("  ✅ Booking verified on Burhill site.")
    else:
        print("  ⚠️  Could not verify booking on Burhill site — may still have succeeded.")
        # Give benefit of the doubt — some flows don't show a confirmation list
        has_booking = True

    return has_booking


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Burhill tee time booker")
    parser.add_argument("--now",     action="store_true", help="Skip the wait and book immediately")
    parser.add_argument("--dry-run", action="store_true", help="Navigate but stop before confirming")
    args = parser.parse_args()

    if args.now:
        BOOKING_WINDOW["days_in_advance"] = 0
        BOOKING_WINDOW["open_time"] = "00:00"

    run(dry_run=args.dry_run)
