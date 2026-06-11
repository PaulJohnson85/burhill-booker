"""Burhill Tee Time Booker — Flask web portal with user accounts."""
import os
import sys
from datetime import datetime, timedelta

# Load .env when running locally
_env = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env):
    for _line in open(_env):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from flask import (Flask, render_template, request, redirect,
                   url_for, jsonify, flash)
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash

import db
import scheduler as sched
import crypto
from open_play import check_booking
from config import BOOKING_WINDOW

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# ── Flask-Login ──────────────────────────────────────────────────────────────

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access Burhill Booker."


class User(UserMixin):
    def __init__(self, row: dict):
        self.id       = row["id"]
        self.name     = row["name"]
        self.email    = row["email"]
        self.is_admin = bool(row.get("is_admin", False))

    def get_id(self):
        return str(self.id)


@login_manager.user_loader
def load_user(user_id):
    row = db.get_user_by_id(int(user_id))
    return User(row) if row else None


# ── Auth routes ──────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        f = request.form
        name          = f["name"].strip()
        email         = f["email"].strip().lower()
        password      = f["password"]
        burhill_user  = f["burhill_user"].strip()
        burhill_pass  = f["burhill_pass"]

        if not all([name, email, password, burhill_user, burhill_pass]):
            flash("All fields are required.", "error")
            return render_template("register.html")
        if len(password) < 8:
            flash("Portal password must be at least 8 characters.", "error")
            return render_template("register.html")
        if db.get_user_by_email(email):
            flash("An account with that email already exists.", "error")
            return render_template("register.html")

        user_id = db.create_user(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
            burhill_user=burhill_user,
            burhill_pass_encrypted=crypto.encrypt(burhill_pass),
        )
        row = db.get_user_by_id(user_id)
        login_user(User(row), remember=True)
        flash(f"Welcome, {name}! Your account is ready.", "success")
        return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email    = request.form["email"].strip().lower()
        password = request.form["password"]
        row = db.get_user_by_email(email)
        if not row or not check_password_hash(row["password_hash"], password):
            flash("Incorrect email or password.", "error")
            return render_template("login.html")
        login_user(User(row), remember=True)
        return redirect(request.args.get("next") or url_for("index"))
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        burhill_user = request.form["burhill_user"].strip()
        burhill_pass = request.form["burhill_pass"]
        if burhill_user and burhill_pass:
            db.update_user_credentials(
                current_user.id, burhill_user, crypto.encrypt(burhill_pass)
            )
            flash("Burhill credentials updated.", "success")
        return redirect(url_for("settings"))
    row = db.get_user_by_id(current_user.id)
    return render_template("settings.html", burhill_user=row.get("burhill_user", ""))


# ── Booking routes ───────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    bookings = db.get_bookings_for_user(current_user.id)
    return render_template(
        "index.html",
        bookings=bookings,
        today=datetime.now().strftime("%Y-%m-%d"),
        booking_days=BOOKING_WINDOW["days_in_advance"],
        booking_time=BOOKING_WINDOW["open_time"],
        user=current_user,
    )


@app.route("/add", methods=["POST"])
@login_required
def add():
    f = request.form
    course         = f["course"]
    players        = int(f["players"])
    date_iso       = f["date"]
    preferred_time = f["preferred_time"]

    dt       = datetime.strptime(date_iso, "%Y-%m-%d")
    date_str = dt.strftime("%d/%m/%Y")
    open_dt  = dt - timedelta(days=BOOKING_WINDOW["days_in_advance"])
    h, m     = map(int, BOOKING_WINDOW["open_time"].split(":"))
    opens_at = open_dt.replace(hour=h, minute=m, second=0, microsecond=0)

    op = check_booking(date_str, course, preferred_time)

    booking_id = db.add_booking(
        course=course, players=players, date=date_str,
        preferred_time=preferred_time, opens_at=opens_at.isoformat(),
        op_status=op["status"], op_message=op["message"],
        user_id=current_user.id,
    )
    sched.schedule_booking(booking_id, opens_at)
    return redirect(url_for("index"))


@app.route("/cancel/<int:booking_id>", methods=["POST"])
@login_required
def cancel(booking_id):
    b = db.get_booking(booking_id)
    if b and (b["user_id"] == current_user.id or current_user.is_admin):
        sched.cancel_booking(booking_id)
        db.update_status(booking_id, "cancelled", message="Cancelled by user")
    return redirect(url_for("index"))


@app.route("/delete/<int:booking_id>", methods=["POST"])
@login_required
def delete(booking_id):
    b = db.get_booking(booking_id)
    if b and (b["user_id"] == current_user.id or current_user.is_admin):
        sched.cancel_booking(booking_id)
        db.delete_booking(booking_id)
    return redirect(url_for("index"))


# ── Admin ────────────────────────────────────────────────────────────────────

@app.route("/admin")
@login_required
def admin():
    if not current_user.is_admin:
        return redirect(url_for("index"))
    return render_template("admin.html",
                           bookings=db.get_all_bookings(),
                           users=db.get_all_users())


# ── API ──────────────────────────────────────────────────────────────────────

@app.route("/api/bookings")
@login_required
def api_bookings():
    return jsonify(db.get_bookings_for_user(current_user.id))


@app.route("/api/open_play_check")
@login_required
def api_open_play_check():
    date_iso = request.args.get("date", "")
    course   = request.args.get("course", "Golf")
    time_str = request.args.get("time", "09:00")
    if not date_iso:
        return jsonify({"status": "no_data", "message": ""})
    try:
        dt       = datetime.strptime(date_iso, "%Y-%m-%d")
        date_str = dt.strftime("%d/%m/%Y")
        return jsonify(check_booking(date_str, course, time_str))
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    sched.init_scheduler()
    port = int(os.environ.get("PORT", 5001))
    print(f"\n🏌️  Burhill Booker running at http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
