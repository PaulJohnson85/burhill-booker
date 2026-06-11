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
import notify
import crypto
from book import _login, _navigate_to_date, _find_slot, _book_slot
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


def run(booking_id: int, dry_run: bool = False):
    row = db.get_booking(booking_id)
    if not row:
        print(f"Booking {booking_id} not found in DB", file=sys.stderr)
        sys.exit(1)

    booking = {
        "course":            row["course"],
        "players":           row["players"],
        "date":              row["date"],
        "preferred_time":    row["preferred_time"],
        "lead_time_minutes": 2,
    }

    # Load per-user Burhill credentials if this booking belongs to a user
    if row.get("user_id"):
        user = db.get_user_by_id(row["user_id"])
        if user and user.get("burhill_user") and user.get("burhill_pass"):
            import os
            os.environ["BURHILL_USERNAME"] = user["burhill_user"]
            os.environ["BURHILL_PASSWORD"] = crypto.decrypt(user["burhill_pass"])
            # Force config to re-read the updated env vars
            from config import CREDENTIALS
            CREDENTIALS["username"] = user["burhill_user"]
            CREDENTIALS["password"] = crypto.decrypt(user["burhill_pass"])

    db.update_status(booking_id, "running", message="Browser launched …")

    try:
        with sync_playwright() as pw:
            import os
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
                _navigate_to_date(page, booking)
                slot_url = _find_slot(page, booking)

                if slot_url is None:
                    msg = "No matching tee time found."
                    db.update_status(booking_id, "failed", message=msg)
                    notify.booking_failed(booking, msg)
                    sys.exit(1)

                m = re.search(r"Start=(\d{2}%3A\d{2})", slot_url)
                slot_time = m.group(1).replace("%3A", ":") if m else "?"
                print(f"✅  Found slot: {slot_time}")

                if dry_run:
                    db.update_status(booking_id, "failed",
                                     message=f"[DRY RUN] Would book {slot_time}")
                    return

                confirmed = _book_slot(page, slot_url)
                if confirmed:
                    db.update_status(
                        booking_id, "booked",
                        slot_time=slot_time,
                        booked_at=datetime.now().isoformat(),
                        message=f"Booked at {slot_time}",
                    )
                    notify.booking_confirmed(booking, slot_time)
                    print(f"✅  Booking {booking_id} confirmed at {slot_time}")
                else:
                    msg = "Booking flow completed but could not verify on Burhill site."
                    db.update_status(booking_id, "failed", message=msg)
                    notify.booking_failed(booking, msg)
                    sys.exit(1)

            except PWTimeout as e:
                msg = f"Timeout: {str(e)[:300]}"
                db.update_status(booking_id, "failed", message=msg)
                notify.booking_failed(booking, msg)
                sys.exit(1)
            finally:
                context.close()
                browser.close()

    except Exception as e:
        msg = str(e)[:300]
        db.update_status(booking_id, "failed", message=msg)
        notify.booking_failed(booking, msg)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--booking-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.booking_id, dry_run=args.dry_run)
