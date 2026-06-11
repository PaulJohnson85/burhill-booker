"""
Sync a user's live bookings from Burhill's book_history.php into the
site_bookings table so the dashboard can show them.

Usage:
    python3 run_sync.py --user-id 1
"""
import argparse
import base64
import json
import os
import sys

import db
import crypto
from book import _login, BASE_URL
from playwright.sync_api import sync_playwright


def _p(msg):
    print(msg, flush=True)


def _decode_payload(payload: str) -> dict:
    try:
        pad = payload + "=" * (-len(payload) % 4)
        decoded = base64.b64decode(pad).decode("utf-8", "replace")
        return json.loads(decoded)
    except Exception:
        return {}


def fetch_site_bookings(page) -> list:
    """Read book_history.php and return upcoming bookings with their refs."""
    page.goto(f"{BASE_URL}/book_history.php", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(800)
    _p(f"  [book_history] → {page.url}")

    forms = page.evaluate("""() =>
        Array.from(document.forms).map(f => {
            const els = {};
            for (const el of Array.from(f.elements)) {
                if (el.name) els[el.name] = el.value;
            }
            return els;
        })
    """)

    cancel_refs = {f.get("ref") for f in forms if f.get("cancel") and f.get("ref")}
    bookings = []
    seen = set()
    for f in forms:
        ref = f.get("rebookref")
        payload = f.get("payload")
        if not ref or not payload or ref in seen:
            continue
        seen.add(ref)
        info = _decode_payload(payload)
        _p(f"  [entry {ref}] {json.dumps(info)[:300]}")
        date = info.get("HistDate", "")
        time_ = info.get("HistTime", "")
        bookings.append({
            "ref": ref,
            "date_text": f"{date} {time_}".strip(),
            "course": info.get("HistCourse", ""),
            "participants": (info.get("HistPeople") or "").strip(),
            "raw": json.dumps(info)[:2000],
            "can_cancel": ref in cancel_refs,
        })
    return bookings


def run(user_id: int):
    _p(f"[run_sync] start user_id={user_id}")
    user = db.get_user_by_id(user_id)
    if not user or not user.get("burhill_user") or not user.get("burhill_pass"):
        print(f"User {user_id} has no Burhill credentials", file=sys.stderr, flush=True)
        sys.exit(1)

    os.environ["BURHILL_USERNAME"] = user["burhill_user"]
    os.environ["BURHILL_PASSWORD"] = crypto.decrypt(user["burhill_pass"])
    from config import CREDENTIALS
    CREDENTIALS["username"] = user["burhill_user"]
    CREDENTIALS["password"] = crypto.decrypt(user["burhill_pass"])

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
            _p("[run_sync] logged in")
            rows = fetch_site_bookings(page)
            db.replace_site_bookings(user_id, rows)
            _p(f"[run_sync] ✅ synced {len(rows)} booking(s) for user {user_id}")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, required=True)
    args = parser.parse_args()
    run(args.user_id)
