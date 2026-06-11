"""Background scheduler — watches the booking queue and fires jobs at the right time."""
import os
import sys
import subprocess
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

import db
from config import BOOKING_WINDOW

_scheduler: BackgroundScheduler = None


def init_scheduler():
    global _scheduler
    jobstores = {
        "default": SQLAlchemyJobStore(url=f"sqlite:///{db.DB_PATH}")
    }
    _scheduler = BackgroundScheduler(jobstores=jobstores, timezone="Europe/London")
    _scheduler.start()

    # Reschedule any pending/waiting bookings that survived a server restart
    for booking in db.get_all_bookings():
        if booking["status"] in ("pending", "waiting") and booking["opens_at"]:
            opens_at = datetime.fromisoformat(booking["opens_at"])
            fire_at = _fire_time(opens_at)
            _add_job(booking["id"], fire_at)


def schedule_booking(booking_id: int, opens_at: datetime):
    fire_at = _fire_time(opens_at)
    _add_job(booking_id, fire_at)


def cancel_booking(booking_id: int):
    if _scheduler:
        job_id = f"booking_{booking_id}"
        try:
            _scheduler.remove_job(job_id)
        except Exception:
            pass


def _fire_time(opens_at: datetime) -> datetime:
    lead = BOOKING_WINDOW.get("lead_time_minutes", 2) if hasattr(BOOKING_WINDOW, "get") else 2
    t = opens_at - timedelta(minutes=lead)
    return max(t, datetime.now() + timedelta(seconds=5))


def _add_job(booking_id: int, fire_at: datetime):
    if not _scheduler:
        return
    _scheduler.add_job(
        _run_booking_subprocess,
        "date",
        run_date=fire_at,
        args=[booking_id],
        id=f"booking_{booking_id}",
        replace_existing=True,
        misfire_grace_time=600,
    )
    db.update_status(booking_id, "waiting",
                     message=f"Scheduled to run at {fire_at:%Y-%m-%d %H:%M:%S}")


def _run_booking_subprocess(booking_id: int):
    db.update_status(booking_id, "running", message="Starting booking process …")
    script = os.path.join(os.path.dirname(__file__), "run_booking.py")
    result = subprocess.run(
        [sys.executable, script, "--booking-id", str(booking_id)],
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        # run_booking.py already called db.update_status("failed") itself,
        # but if it crashed before that, set it here.
        row = db.get_booking(booking_id)
        if row and row["status"] not in ("failed", "booked"):
            db.update_status(booking_id, "failed",
                             message=(result.stderr or "Unknown error")[-400:])
