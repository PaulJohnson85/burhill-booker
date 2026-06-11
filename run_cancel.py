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


def _read_history_forms(page):
    """Return each form on book_history.php as a dict of its named element values."""
    return page.evaluate("""() =>
        Array.from(document.forms).map(f => {
            const els = {};
            for (const el of Array.from(f.elements)) {
                if (el.name) els[el.name] = el.value;
            }
            return {action: f.action || '', els: els};
        })
    """)


def _find_cancel_ref(forms, date_str, slot_time):
    """
    book_history.php has, per booking:
      - a rebook form: hidden payload (base64 JSON incl. HistDate) + rebookref
      - a cancel form: hidden ref + cancel=1
    Decode the payloads, match the booking date (and slot time when present),
    return the matching ref.
    """
    import base64, json
    d, m, y = date_str.split("/")
    date_keys = [f"{d}/{m}/{y}", f"{d}/{m}/{y[2:]}"]

    candidates = []
    for f in forms:
        els = f["els"]
        payload = els.get("payload")
        ref = els.get("rebookref") or els.get("ref")
        if not payload or not ref:
            continue
        try:
            pad = payload + "=" * (-len(payload) % 4)
            decoded = base64.b64decode(pad).decode("utf-8", "replace").replace("\\/", "/")
        except Exception as e:
            _p(f"    [payload decode failed for ref {ref}: {e}]")
            continue
        _p(f"    [history entry ref={ref}] {decoded[:200]}")
        if any(k in decoded for k in date_keys):
            score = 1
            if slot_time and slot_time in decoded:
                score = 2
            candidates.append((score, ref, decoded))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    if slot_time and candidates[0][0] == 1 and len(candidates) > 1:
        _p(f"  ⚠️  Multiple bookings on {date_str}, none matched time {slot_time} exactly "
           f"— using first")
    return candidates[0][1]


def _cancel_on_site(page, booking) -> bool:
    """Find the booking in Booking History and cancel it. Returns True on success."""
    date_str  = booking["date"]            # DD/MM/YYYY
    slot_time = booking.get("slot_time")   # HH:MM or None

    # The Cancel button pops a JS confirm() dialog — Playwright dismisses
    # dialogs by default, which silently aborts the submit. Accept them.
    page.on("dialog", lambda dialog: (
        _p(f"  [dialog: {dialog.type} '{dialog.message[:80]}' — accepting]"),
        dialog.accept()))

    # 1. Open booking history
    page.goto(f"{BASE_URL}/book_history.php", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(800)
    _p(f"  [book_history] → {page.url}")
    _dump_forms(page, "book_history page")

    # 2. Cancellation is form-based: find the booking's ref via the rebook
    #    payload (base64 JSON containing HistDate), then submit the form
    #    that has ref=<ref> and cancel=1.
    forms = _read_history_forms(page)
    ref = _find_cancel_ref(forms, date_str, slot_time)
    if ref is None:
        page.screenshot(path="cancel_not_found.png")
        raise RuntimeError(f"No history entry found for booking on {date_str}"
                           f"{' at ' + slot_time if slot_time else ''}")
    _p(f"  Booking ref: {ref}")

    try:
        how = page.evaluate("""(ref) => {
            const form = Array.from(document.forms).find(f => {
                let r = null, cancel = false;
                for (const el of Array.from(f.elements)) {
                    if (el.name === 'ref') r = el.value;
                    if (el.name === 'cancel') cancel = true;
                }
                return cancel && r === ref;
            });
            if (!form) return 'no-cancel-form';
            const btn = Array.from(form.elements).find(el =>
                el.type === 'submit' || el.type === 'image');
            if (btn) { btn.click(); return 'clicked'; }
            form.submit();
            return 'js-submit';
        }""", ref)
        _p(f"  [cancel form submit: {how}]")
        if how == "no-cancel-form":
            raise RuntimeError(f"No cancel form found for ref {ref}")
    except Exception as e:
        if "no-cancel-form" in str(e):
            raise
        _p(f"  [cancel submit] evaluate raised (navigation): {str(e)[:80]}")
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    page.wait_for_timeout(800)
    _p(f"  [after cancel submit] → {page.url}")

    # The response may be a confirmation page (same URL, different content).
    # Log what came back and submit any confirm form that mentions our ref.
    try:
        body = page.evaluate("() => document.body.innerText.trim().slice(0, 600)")
        _p(f"  [response text] {body}")
    except Exception:
        pass
    # The response shows a "Do you want to cancel this booking? … Yes / No"
    # overlay whose Yes is a button/link, not a form submit. Click "Yes".
    try:
        how = page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll(
                'button, a, input[type=button], input[type=submit], span[onclick], div[onclick]'));
            const yes = els.find(el =>
                ((el.innerText || el.value || '').trim().toLowerCase() === 'yes'));
            if (!yes) return 'no-yes-control';
            yes.click();
            return 'clicked-yes:' + yes.tagName + (yes.getAttribute('onclick') ?
                ' onclick=' + yes.getAttribute('onclick').slice(0, 80) : '');
        }""")
        _p(f"  [confirm Yes: {how}]")
        if how == "no-yes-control":
            # Log clickable candidates for debugging
            cands = page.evaluate("""() =>
                Array.from(document.querySelectorAll('button, a, input[type=button], [onclick]'))
                    .map(el => `${el.tagName} text='${(el.innerText || el.value || '').trim().slice(0,30)}'`)
                    .slice(0, 40)
            """)
            _p(f"  [clickables] {cands}")
    except Exception as e:
        _p(f"  [confirm Yes] evaluate raised (navigation): {str(e)[:80]}")
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    page.wait_for_timeout(1200)
    _p(f"  [after Yes] → {page.url}")
    try:
        body = page.evaluate("() => document.body.innerText.trim().slice(0, 400)")
        _p(f"  [post-Yes text] {body}")
    except Exception:
        pass

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

    # 4. Verify: reload history and check the cancel form for this ref is gone
    page.goto(f"{BASE_URL}/book_history.php", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(800)
    forms_after = _read_history_forms(page)
    still_there = any(
        f["els"].get("cancel") and (f["els"].get("ref") == ref)
        for f in forms_after)
    if still_there:
        _p(f"  ⚠️  Cancel form for ref {ref} still present — cancel may not have completed")
        page.screenshot(path="cancel_unverified.png")
        return False
    _p(f"  ✅ Ref {ref} no longer cancellable in history — cancelled.")
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
