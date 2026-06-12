"""Background scheduler — watches the booking queue and fires jobs at the right time."""
import os
import sys
import subprocess
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.util import datetime_to_utc_timestamp

import db
from config import BOOKING_WINDOW

_scheduler: BackgroundScheduler = None

# Use UTC everywhere internally — avoids any server/DST ambiguity.
# The 07:00 booking window is expressed in Europe/London time in app.py;
# we convert it to UTC here before handing it to APScheduler.
try:
    from zoneinfo import ZoneInfo
    _LONDON = ZoneInfo("Europe/London")
except Exception:
    _LONDON = timezone.utc


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    """Convert a naive datetime (assumed London local) to UTC-aware."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    # Attach London tz then convert to UTC
    try:
        return dt.replace(tzinfo=_LONDON).astimezone(timezone.utc)
    except Exception:
        return dt.replace(tzinfo=timezone.utc)


def init_scheduler():
    global _scheduler
    jobstores = {
        "default": SQLAlchemyJobStore(url=db._db_url())
    }
    _scheduler = BackgroundScheduler(jobstores=jobstores, timezone=timezone.utc)
    _scheduler.start()

    # Reschedule any pending/waiting bookings that survived a server restart
    for booking in db.get_all_bookings():
        if booking["status"] in ("pending", "waiting") and booking["opens_at"]:
            opens_at = datetime.fromisoformat(booking["opens_at"])
            fire_at  = _fire_time(opens_at)
            _add_job(booking["id"], fire_at)

    # Daily housekeeping: remove cancelled booking records
    _scheduler.add_job(
        _cleanup_cancelled,
        "interval",
        hours=24,
        id="cleanup_cancelled",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Drop the old mailbox-polling job if it survived in the jobstore
    try:
        _scheduler.remove_job("mail_import")
    except Exception:
        pass

    # Fetch the open play PDF from the members' website daily
    _scheduler.add_job(
        _fetch_open_play,
        "interval",
        hours=24,
        next_run_time=_now_utc() + timedelta(minutes=3),
        id="openplay_fetch",
        replace_existing=True,
        misfire_grace_time=3600,
    )


def _fetch_open_play():
    # Subprocess: it drives a headless browser, keep it out of the web process
    script = os.path.join(os.path.dirname(__file__), "run_openplay_fetch.py")
    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=300,
        )
        if result.stdout:
            print(f"[openplay_fetch stdout]\n{result.stdout}", flush=True)
        if result.stderr:
            print(f"[openplay_fetch stderr]\n{result.stderr}", flush=True)
    except subprocess.TimeoutExpired:
        print("[openplay_fetch] timed out after 5 minutes", flush=True)


def _cleanup_cancelled():
    removed = db.delete_bookings_by_status("cancelled")

    # Failed bookings age out after 3 days (measured from when the booking
    # ran — opens_at — falling back to when it was created)
    cutoff = datetime.now() - timedelta(days=3)
    aged = 0
    for b in db.get_all_bookings():
        if b["status"] not in ("failed", "no_slots"):
            continue
        ts = b.get("opens_at") or b.get("created_at")
        try:
            when = datetime.fromisoformat(ts)
        except Exception:
            continue
        if when < cutoff:
            db.delete_booking(b["id"])
            aged += 1

    print(f"[cleanup] removed {removed} cancelled, {aged} failed (>3 days) booking(s)",
          flush=True)


def schedule_booking(booking_id: int, opens_at: datetime):
    fire_at = _fire_time(opens_at)
    _add_job(booking_id, fire_at)


def cancel_booking(booking_id: int):
    if _scheduler:
        try:
            _scheduler.remove_job(f"booking_{booking_id}")
        except Exception:
            pass


def _fire_time(opens_at: datetime) -> datetime:
    """Return UTC-aware fire time: opens_at minus lead, but never before now+5s."""
    lead = BOOKING_WINDOW.get("lead_time_minutes", 2)
    opens_utc = _to_utc(opens_at)
    t = opens_utc - timedelta(minutes=lead)
    return max(t, _now_utc() + timedelta(seconds=5))


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
    # Show London local time in the UI message
    try:
        local_time = fire_at.astimezone(_LONDON).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        local_time = fire_at.strftime("%Y-%m-%d %H:%M:%S")
    db.update_status(booking_id, "waiting",
                     message=f"Scheduled to run at {local_time} (London)")


def _run_booking_subprocess(booking_id: int):
    db.update_status(booking_id, "running", message="Starting booking process …")
    script = os.path.join(os.path.dirname(__file__), "run_booking.py")
    try:
        result = subprocess.run(
            [sys.executable, script, "--booking-id", str(booking_id)],
            capture_output=True,
            text=True,
            timeout=600,  # 10-minute hard limit
        )
    except subprocess.TimeoutExpired:
        db.update_status(booking_id, "failed",
                         message="Booking subprocess timed out after 10 minutes")
        return

    if result.stdout:
        print(f"[booking {booking_id} stdout]\n{result.stdout}", flush=True)
    if result.stderr:
        print(f"[booking {booking_id} stderr]\n{result.stderr}", flush=True)

    if result.returncode != 0:
        row = db.get_booking(booking_id)
        if row and row["status"] not in ("failed", "booked", "no_slots", "cancelled"):
            combined = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
            # Pass None when combined is empty — COALESCE keeps the last meaningful message
            msg = combined[-2000:] if combined else None
            db.update_status(booking_id, "failed",
                             message=msg or f"Process exited {result.returncode} with no output")
