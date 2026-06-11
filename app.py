"""
Burhill Tee Time Booker — Flask web portal.

Usage:
    python3 app.py
    Then open http://localhost:5000
"""
from datetime import datetime, timedelta
import os

# Load .env when running locally (Railway injects env vars directly)
_env = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env):
    for _line in open(_env):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from flask import Flask, render_template, request, redirect, url_for, jsonify

import db
import scheduler as sched
from open_play import check_booking
from config import BOOKING_WINDOW

app = Flask(__name__)


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    bookings = db.get_all_bookings()
    return render_template(
        "index.html",
        bookings=bookings,
        today=datetime.now().strftime("%Y-%m-%d"),
        booking_days=BOOKING_WINDOW["days_in_advance"],
        booking_time=BOOKING_WINDOW["open_time"],
    )


@app.route("/add", methods=["POST"])
def add():
    f = request.form
    course         = f["course"]
    players        = int(f["players"])
    date_iso       = f["date"]           # YYYY-MM-DD from <input type="date">
    preferred_time = f["preferred_time"] # HH:MM

    # Convert to DD/MM/YYYY used throughout the system
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    date_str = dt.strftime("%d/%m/%Y")

    # Calculate booking window open time
    open_dt = dt - timedelta(days=BOOKING_WINDOW["days_in_advance"])
    h, m = map(int, BOOKING_WINDOW["open_time"].split(":"))
    opens_at = open_dt.replace(hour=h, minute=m, second=0, microsecond=0)

    # Open play check
    op = check_booking(date_str, course, preferred_time)

    booking_id = db.add_booking(
        course=course,
        players=players,
        date=date_str,
        preferred_time=preferred_time,
        opens_at=opens_at.isoformat(),
        op_status=op["status"],
        op_message=op["message"],
    )

    sched.schedule_booking(booking_id, opens_at)
    return redirect(url_for("index"))


@app.route("/cancel/<int:booking_id>", methods=["POST"])
def cancel(booking_id):
    sched.cancel_booking(booking_id)
    db.update_status(booking_id, "cancelled", message="Cancelled by user")
    return redirect(url_for("index"))


@app.route("/delete/<int:booking_id>", methods=["POST"])
def delete(booking_id):
    sched.cancel_booking(booking_id)
    db.delete_booking(booking_id)
    return redirect(url_for("index"))


@app.route("/api/bookings")
def api_bookings():
    """Polled by the dashboard every 5 s to refresh status badges."""
    return jsonify(db.get_all_bookings())


@app.route("/api/open_play_check")
def api_open_play_check():
    date_iso = request.args.get("date", "")
    course   = request.args.get("course", "Golf")
    time_str = request.args.get("time", "09:00")
    if not date_iso:
        return jsonify({"status": "no_data", "message": ""})
    try:
        dt = datetime.strptime(date_iso, "%Y-%m-%d")
        date_str = dt.strftime("%d/%m/%Y")
        result = check_booking(date_str, course, time_str)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from config import CREDENTIALS
    if not CREDENTIALS["username"] or not CREDENTIALS["password"]:
        print("⚠️  Set BURHILL_USERNAME and BURHILL_PASSWORD environment variables before starting.")
        import sys; sys.exit(1)
    db.init_db()
    sched.init_scheduler()
    port = int(os.environ.get("PORT", 5001))
    print(f"\n🏌️  Burhill Booker running at http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
