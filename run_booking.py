"""
Called by the scheduler to execute a single booking.
Reads booking details from the DB, runs the Playwright flow, writes status back.

Usage:
    python3 run_booking.py --booking-id 3
    python3 run_booking.py --booking-id 3 --dry-run
"""
import argparse
import re
import sys
from datetime import datetime

import db
from config import CREDENTIALS, BOOKING_WINDOW
from book import _login, _navigate_to_date, _find_slot, _book_slot
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


def run(booking_id: int, dry_run: bool = False):
    row = db.get_booking(booking_id)
    if not row:
        print(f"Booking {booking_id} not found in DB", file=sys.stderr)
        sys.exit(1)

    booking = {
        "course":         row["course"],
        "players":        row["players"],
        "date":           row["date"],
        "preferred_time": row["preferred_time"],
        "lead_time_minutes": 2,
    }

    db.update_status(booking_id, "running", message="Browser launched …")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            page = browser.new_page()
            try:
                _login(page)
                _navigate_to_date(page, booking)
                slot_url = _find_slot(page, booking)

                if slot_url is None:
                    page.screenshot(path=f"no_slot_{booking_id}.png")
                    db.update_status(booking_id, "failed",
                                     message="No matching tee time found.")
                    sys.exit(1)

                m = re.search(r"Start=(\d{2}%3A\d{2})", slot_url)
                slot_time = m.group(1).replace("%3A", ":") if m else "?"
                print(f"✅  Found slot: {slot_time}")

                if dry_run:
                    db.update_status(booking_id, "failed",
                                     message=f"[DRY RUN] Would book {slot_time}")
                    return

                _book_slot(page, slot_url)
                db.update_status(
                    booking_id, "booked",
                    slot_time=slot_time,
                    booked_at=datetime.now().isoformat(),
                    message=f"Booked at {slot_time}",
                )
                print(f"✅  Booking {booking_id} confirmed at {slot_time}")

            except PWTimeout as e:
                page.screenshot(path=f"error_{booking_id}.png")
                db.update_status(booking_id, "failed",
                                 message=f"Timeout: {str(e)[:300]}")
                sys.exit(1)
            finally:
                browser.close()

    except Exception as e:
        db.update_status(booking_id, "failed", message=str(e)[:300])
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--booking-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.booking_id, dry_run=args.dry_run)
