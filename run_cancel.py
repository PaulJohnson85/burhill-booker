"""
Cancel a booking on the Burhill site.
Reads booking details from the DB, runs the Playwright cancel flow,
writes status back.

Usage:
    python3 run_cancel.py --booking-id 32
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime

import db
import crypto
from book import _login, _dump_forms, BASE_URL
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


def _p(msg):
    print(msg, flush=True)


def _date_variants(date_str: str):
    """ '16/06/2026' → ['16/06/2026', '16/06/26', '16%2F06%2F26', '16%2F06%2F2026'] """
    d, m, y = date_str.split("/")
    return [
        f"{d}/{m}/{y}", f"{d}/{m}/{y[2:]}",
        f"{d}%2F{m}%2F{y[2:]}", f"{d}%2F{m}%2F{y}",
    ]


def _cancel_on_site(page, booking) -> bool:
    """Find the booking in Booking History and cancel it. Returns True on success."""
    date_str  = booking["date"]            # DD/MM/YYYY
    slot_time = booking.get("slot_time")   # HH:MM or None
    variants  = _date_variants(date_str)

    # 1. Open booking history
    page.goto(f"{BASE_URL}/book_history.php", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(800)
    _p(f"  [book_history] → {page.url}")

    # Log all links so we can see the page structure
    links = page.evaluate("""() =>
        Array.from(document.querySelectorAll('a')).map(a =>
            ({href: a.getAttribute('href') || '', text: (a.innerText || '').trim().slice(0, 60)}))
    """)
    for l in links:
        if l["href"]:
            _p(f"    [history link] text='{l['text']}' href={l['href'][:120]}")
    _dump_forms(page, "book_history page")

    # 2. Find the cancel link for this booking.
    #    Prefer a book_cancel link whose href mentions the booking's date
    #    (and slot time if we have one); fall back to any single cancel link.
    cancel_links = [l for l in links if "cancel" in l["href"].lower()
                    or "cancel" in l["text"].lower()]
    _p(f"  Found {len(cancel_links)} cancel link(s)")

    def matches_booking(l):
        target = (l["href"] + " " + l["text"])
        if not any(v in target for v in variants):
            return False
        if slot_time:
            t_enc = slot_time.replace(":", "%3A")
            if slot_time not in target and t_enc not in target:
                return False
        return True

    chosen = next((l for l in cancel_links if matches_booking(l)), None)
    if chosen is None and len(cancel_links) == 1:
        _p("  No date-matched link — using the only cancel link on the page")
        chosen = cancel_links[0]
    if chosen is None:
        # Maybe cancellation lives behind a per-booking detail link — try those
        detail = [l for l in links
                  if any(v in l["href"] for v in variants)]
        if detail:
            href = detail[0]["href"]
            if not href.startswith("http"):
                href = f"{BASE_URL}/{href.lstrip('/')}"
            _p(f"  Opening booking detail: {href[:120]}")
            page.goto(href, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(800)
            links2 = page.evaluate("""() =>
                Array.from(document.querySelectorAll('a')).map(a =>
                    ({href: a.getAttribute('href') || '', text: (a.innerText || '').trim().slice(0, 60)}))
            """)
            for l in links2:
                if l["href"]:
                    _p(f"    [detail link] text='{l['text']}' href={l['href'][:120]}")
            _dump_forms(page, "booking detail page")
            chosen = next((l for l in links2 if "cancel" in l["href"].lower()
                           or "cancel" in l["text"].lower()), None)

    if chosen is None:
        page.screenshot(path="cancel_not_found.png")
        raise RuntimeError(f"No cancel link found for booking on {date_str}"
                           f"{' at ' + slot_time if slot_time else ''}")

    href = chosen["href"]
    if not href.startswith("http"):
        href = f"{BASE_URL}/{href.lstrip('/')}"
    _p(f"  Cancel link: {href[:150]}")
    page.goto(href, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(800)
    _p(f"  [after cancel link] → {page.url}")

    # 3. The cancel page usually asks for confirmation — submit its own form
    #    (filtering out the side-menu forms), for up to 3 steps.
    for step in range(3):
        cur = page.url
        content = page.content().lower()
        if "cancel" not in cur and ("book_history" in cur or "home.php" in cur
                                    or "complete" in cur):
            break
        _dump_forms(page, f"cancel confirm step {step}")
        try:
            how = page.evaluate("""() => {
                const menu = ['home.php','logout.php','book_start','book_back',
                              'book_history','book_basket_view','player_details',
                              'player_changepassword','player_attachments','club_details'];
                const forms = Array.from(document.forms).filter(f =>
                    !menu.some(m => (f.action || '').includes(m)));
                // Prefer a button whose value mentions cancel/confirm/yes
                for (const f of forms) {
                    const btn = Array.from(f.elements).find(el =>
                        (el.type === 'submit' || el.type === 'image') &&
                        /cancel|confirm|yes|submit/i.test(el.value || el.name || ''));
                    if (btn) { btn.click(); return 'clicked:' + (btn.value || btn.name); }
                }
                const f = forms[0];
                if (!f) return 'no-form';
                const btn = Array.from(f.elements).find(el =>
                    el.type === 'submit' || el.type === 'image');
                if (btn) { btn.click(); return 'clicked:' + (btn.value || btn.name); }
                f.submit();
                return 'js-submit';
            }""")
            _p(f"  [cancel confirm step {step}: {how}]")
            if how == "no-form":
                break
        except Exception as e:
            _p(f"  [cancel confirm step {step}] evaluate raised (navigation): {str(e)[:80]}")
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
        page.wait_for_timeout(800)
        _p(f"  [after confirm step {step}] → {page.url}")

    # 4. Verify: reload history and check the booking is gone
    page.goto(f"{BASE_URL}/book_history.php", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(800)
    content = page.content()
    still_there = any(v in content for v in variants[:2]) and (
        not slot_time or slot_time in content)
    if still_there:
        _p("  ⚠️  Booking still appears in history — cancel may not have completed")
        page.screenshot(path="cancel_unverified.png")
        return False
    _p("  ✅ Booking no longer in history — cancelled.")
    return True


def run(booking_id: int):
    _p(f"[run_cancel] start booking_id={booking_id}")
    row = db.get_booking(booking_id)
    if not row:
        print(f"Booking {booking_id} not found in DB", file=sys.stderr, flush=True)
        sys.exit(1)

    if row.get("user_id"):
        user = db.get_user_by_id(row["user_id"])
        if user and user.get("burhill_user") and user.get("burhill_pass"):
            os.environ["BURHILL_USERNAME"] = user["burhill_user"]
            os.environ["BURHILL_PASSWORD"] = crypto.decrypt(user["burhill_pass"])
            from config import CREDENTIALS
            CREDENTIALS["username"] = user["burhill_user"]
            CREDENTIALS["password"] = crypto.decrypt(user["burhill_pass"])
            _p(f"[run_cancel] credentials set for user {user.get('burhill_user')}")

    db.update_status(booking_id, "cancelling", message="Cancelling on Burhill site …")

    try:
        with sync_playwright() as pw:
            headless = os.environ.get("HEADLESS", "0") == "1"
            browser = pw.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            try:
                _login(page)
                _p("[run_cancel] logged in")
                ok = _cancel_on_site(page, row)
                if ok:
                    db.update_status(booking_id, "cancelled",
                                     message=f"Cancelled on Burhill site "
                                             f"{datetime.now():%d/%m %H:%M}")
                    _p(f"[run_cancel] ✅ booking {booking_id} cancelled")
                else:
                    db.update_status(booking_id, "booked",
                                     message="Cancel attempt could not be verified — "
                                             "check the Burhill site")
                    sys.exit(1)
            finally:
                context.close()
                browser.close()
    except Exception as e:
        import traceback
        _p(f"[run_cancel] EXCEPTION: {e}")
        _p(traceback.format_exc())
        db.update_status(booking_id, "booked",
                         message=f"Cancel failed: {type(e).__name__}: {str(e)[:200]}")
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--booking-id", type=int, required=True)
    args = parser.parse_args()
    run(args.booking_id)
