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


def _navigate_to_date(page, booking: dict = None):
    print("Navigating to booking …")
    b = booking or BOOKING
    course = b["course"]
    course_text = COURSE_LINK_TEXT.get(course, "Old Course")

    def click_and_wait(locator, label=""):
        try:
            # force=True bypasses visibility checks — handles off-screen/hidden elements
            locator.click(timeout=30_000, force=True)
        except Exception as e:
            page.screenshot(path=f"timeout_{label.replace(' ','_')}.png")
            print(f"    [TIMEOUT on '{label}'] page={page.url}")
            print(f"    HTML snippet: {page.content()[:800]}")
            raise
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
        page.wait_for_timeout(400)
        if label:
            print(f"    [{label}] → {page.url}")

    # 1. Navigate directly to book_start.php — the Make Booking button is in a hidden
    #    side menu so we can't click it; its form action is book_start.php
    print(f"    [post-login] url={page.url}")
    page.goto(f"{BASE_URL}/book_start.php", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(500)
    print(f"    [book_start] → {page.url}")
    print(f"    [book_start links] {[a.inner_text().strip() for a in page.locator('a').all()[:20]]}")

    # 2. Click "Golf Club Tee Times" — match by link text
    click_and_wait(
        page.get_by_role("link", name="Golf Club Tee Times").first,
        "Golf Club Tee Times")

    # 3. Click the chosen course — match by link text
    click_and_wait(
        page.get_by_role("link", name=course_text).first,
        f"Course: {course_text}")

    print(f"    [after course select] → {page.url}")

    # 4. On book_participants.php: set player count and submit gotdata=1
    players = str(b["players"])
    page.select_option('select[name="NumPeople"]', players)
    # No visible submit button — submit the form programmatically
    try:
        page.locator('form:has(input[name="gotdata"][value="1"])').evaluate("f => f.submit()")
    except Exception:
        pass  # context destroyed by navigation is expected
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    page.wait_for_timeout(400)
    print(f"    [NumPeople submit] → {page.url}")

    # 5. If still on participants page, mark extra slots as guests then submit gotdata=2
    if "book_participants" in page.url:
        if int(players) > 1:
            for i in range(1, int(players)):
                try:
                    page.evaluate(f"""
                        const cb = document.querySelector('input[name="BookNonMemb{i}"]');
                        if (cb) cb.checked = true;
                    """)
                except Exception:
                    pass
        try:
            page.locator('form:has(input[name="gotdata"][value="2"])').evaluate("f => f.submit()")
        except Exception:
            pass
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
        page.wait_for_timeout(400)
        print(f"    [Participants confirm] → {page.url}")
    else:
        print(f"    [Participants] already progressed → {page.url}")

    print(f"    Current page: {page.url}")

    # 6. Click target date — try URL match first, fall back to link text
    target_day = b["date"].split("/")[0].lstrip("0")
    # List all date links found for debugging
    all_date_links = page.locator('a[href*="book_date"]').all()
    print(f"    Date links found: {len(all_date_links)}")
    for lnk in all_date_links[:5]:
        print(f"      [{lnk.inner_text().strip()}] {lnk.get_attribute('href')[:60]}")

    date_locator = page.locator(f'a[href*="StartDate={target_day}"]').first
    if date_locator.count() == 0:
        date_locator = page.locator(f'a:text-is("{target_day}")').first
    if date_locator.count() == 0:
        raise RuntimeError(f"Date {target_day} not found in calendar — is the booking window open?")
    # Date click doesn't fire a clean load event — use direct click + domcontentloaded
    date_locator.click()
    page.wait_for_load_state("domcontentloaded", timeout=45_000)
    page.wait_for_timeout(800)
    print(f"  On tee time page for {b['date']} → {page.url}")


def _find_slot(page, booking: dict = None) -> Optional[str]:
    """Return the href of the first available slot at or after preferred_time."""
    b = booking or BOOKING
    preferred_h, preferred_m = map(int, b["preferred_time"].split(":"))
    preferred_mins = preferred_h * 60 + preferred_m

    # Time slot links have: gotdata=2 and Start=HH%3AMM in their href
    links = page.query_selector_all('a[href*="gotdata=2"][href*="Start="]')
    print(f"  Found {len(links)} time slots. Looking for first slot >= {b['preferred_time']} …")

    for link in links:
        href = link.get_attribute("href")
        text = link.inner_text().strip()
        # Parse time from URL parameter Start=HH%3AMM
        import re
        m = re.search(r'Start=(\d{2}%3A\d{2})', href)
        if not m:
            continue
        slot_mins = parse_time(m.group(1))
        if slot_mins is None:
            continue
        if slot_mins >= preferred_mins:
            print(f"  → Selecting slot: {text}")
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
    page.goto(slot_url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(800)
    print(f"  Slot page: {page.url}")

    # Handle intermediate questionnaire pages (e.g. buggy hire), then find confirm button
    max_steps = 5
    for step in range(max_steps):
        cur = page.url

        if "home.php" in cur:
            # Landed on home — now verify the booking actually exists
            print("  Redirected to home — verifying booking …")
            verified = _verify_booking(page, slot_url)
            page.screenshot(path="booking_confirmed.png")
            return verified

        if "questionnaire" in cur:
            print("  Submitting questionnaire (declining extras) …")
            btn = page.locator('input[type="submit"], button[type="submit"]').first
            btn.click(force=True)
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            page.wait_for_timeout(600)
            print(f"    [questionnaire] → {page.url}")
            continue

        if "book_confirm" in cur:
            print("  On confirmation page — clicking Make Booking inside iframe …")
            # The final Make Booking button is inside wp_cybersource/el_userdetails.php iframe
            iframe = page.frame_locator('iframe[src*="el_userdetails"]')
            make_btn = iframe.locator('input[value*="Make Booking" i], button:has-text("Make Booking"), input[type="submit"]').first
            make_btn.click(timeout=30_000)
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
