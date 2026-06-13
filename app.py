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
        cdh = (f.get("cdh_number") or "").strip()
        if cdh:
            db.update_user_cdh(user_id, cdh)
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
    return render_template("settings.html", burhill_user=row.get("burhill_user", ""),
                           cdh_number=row.get("cdh_number") or "",
                           handicap=row.get("handicap") or "",
                           handicap_updated=(row.get("handicap_updated") or "")[:16].replace("T", " "),
                           current_year=datetime.now().year)


@app.route("/settings/cdh", methods=["POST"])
@login_required
def settings_cdh():
    cdh = (request.form.get("cdh_number") or "").strip()
    db.update_user_cdh(current_user.id, cdh)
    flash("CDH number saved." if cdh else "CDH number cleared.", "success")
    return redirect(url_for("settings"))


@app.route("/refresh_handicaps", methods=["POST"])
@login_required
def refresh_handicaps():
    """Run the WHS handicap refresh now (background)."""
    if not _SITE_BUSY.get(current_user.id):
        _SITE_BUSY[current_user.id] = "handicaps"
        _run_site_subprocess(
            current_user.id,
            ["run_handicaps.py"],
            "handicap refresh")
        flash("Handicap refresh started — check back in a minute.", "success")
    return redirect(url_for("settings"))


@app.route("/openplay_upload", methods=["POST"])
@login_required
def openplay_upload():
    """Manually import an open play PDF (e.g. forwarded from the club email)."""
    f = request.files.get("pdf")
    if not f or not f.filename.lower().endswith(".pdf"):
        flash("Please choose a PDF file.", "error")
        return redirect(url_for("settings"))
    try:
        year = int(request.form.get("year") or datetime.now().year)
    except ValueError:
        year = datetime.now().year

    import tempfile
    from open_play import parse_pdf
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        f.save(tmp.name)
        path = tmp.name
    try:
        schedule = parse_pdf(path, year)
        if schedule:
            db.upsert_open_play(schedule)
            first = next(iter(schedule))
            flash(f"Imported {len(schedule)} open play day(s) "
                  f"for {first.split('/')[1]}/{year}.", "success")
        else:
            flash("No open play table found in that PDF.", "error")
    except Exception as e:
        flash(f"Import failed: {str(e)[:150]}", "error")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return redirect(url_for("settings"))


# ── Booking routes ───────────────────────────────────────────────────────────

def _max_booking_date() -> "datetime":
    """Bookings may only be scheduled while the open play schedule is known —
    otherwise the booker can't tell whether the course is open play at the
    chosen time. The cap is the latest date in the imported schedule
    (falling back to one month ahead if no data is loaded at all)."""
    from open_play import _load_all
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    latest = None
    for key in _load_all():
        try:
            d = datetime.strptime(key, "%d/%m/%Y")
        except ValueError:
            continue
        if d >= today and (latest is None or d > latest):
            latest = d
    if latest:
        return latest
    import calendar as _cal
    y = today.year + (1 if today.month == 12 else 0)
    m = 1 if today.month == 12 else today.month + 1
    day = min(today.day, _cal.monthrange(y, m)[1])
    return datetime(y, m, day)


def _build_calendar(bookings, site_bookings):
    """Two month grids (this month + next) with per-day markers:
    queued/booked portal bookings, live site bookings, open play days."""
    import calendar as _cal
    from open_play import _load_all

    op_data = _load_all()  # keyed DD/MM/YYYY

    queued, booked = {}, {}
    for b in bookings:
        if b["status"] in ("pending", "waiting", "running"):
            queued.setdefault(b["date"], []).append(b["preferred_time"])
        elif b["status"] == "booked":
            booked.setdefault(b["date"], []).append(b["slot_time"] or b["preferred_time"])

    site = {}
    for sb in site_bookings:
        # date_text is "DD/MM/YY HH:MM"
        parts = (sb.get("date_text") or "").split()
        if not parts:
            continue
        d = parts[0]
        try:
            dd, mm, yy = d.split("/")
            key = f"{dd}/{mm}/20{yy}" if len(yy) == 2 else d
        except ValueError:
            continue
        site.setdefault(key, []).append(parts[1] if len(parts) > 1 else "")

    now = datetime.now()
    months = []
    y, m = now.year, now.month
    for _ in range(2):
        weeks = []
        for week in _cal.monthcalendar(y, m):
            row = []
            for day in week:
                if day == 0:
                    row.append(None)
                    continue
                key = f"{day:02d}/{m:02d}/{y}"
                op = op_data.get(key) or {}
                op_course = (op.get("open_play_course") or "").strip()
                # Open play runs daily, alternating courses — show which one
                op_letter = ""
                if "new" in op_course.lower():
                    op_letter = "N"
                elif "old" in op_course.lower():
                    op_letter = "O"
                elif op_course:
                    op_letter = op_course[0].upper()
                row.append({
                    "day": day,
                    "is_today": (day == now.day and m == now.month and y == now.year),
                    "is_past": datetime(y, m, day) < datetime(now.year, now.month, now.day),
                    "queued": queued.get(key),
                    "booked": booked.get(key),
                    "site": site.get(key),
                    "open_play": op.get("open_play_course"),
                    "open_play_letter": op_letter,
                    "open_play_times": op.get("open_play_times"),
                    "event": op.get("event"),
                })
            weeks.append(row)
        months.append({"name": f"{_cal.month_name[m]} {y}", "weeks": weeks})
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


@app.route("/")
@login_required
def index():
    bookings = db.get_bookings_for_user(current_user.id)
    site_bookings = db.get_site_bookings(current_user.id)
    return render_template(
        "index.html",
        bookings=bookings,
        site_bookings=site_bookings,
        site_busy=_SITE_BUSY.get(current_user.id),
        calendar_months=_build_calendar(bookings, site_bookings),
        today=datetime.now().strftime("%Y-%m-%d"),
        max_date=_max_booking_date().strftime("%Y-%m-%d"),
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
    max_dt   = _max_booking_date()
    if dt > max_dt:
        flash(f"The open play schedule is only known up to {max_dt:%d/%m/%Y} — "
              f"bookings beyond that can't check for open play. The next "
              f"calendar is imported on the 1st of the month.", "error")
        return redirect(url_for("index"))
    date_str = dt.strftime("%d/%m/%Y")
    open_dt  = dt - timedelta(days=BOOKING_WINDOW["days_in_advance"])
    h, m     = map(int, BOOKING_WINDOW["open_time"].split(":"))
    opens_at = open_dt.replace(hour=h, minute=m, second=0, microsecond=0)

    op = check_booking(date_str, course, preferred_time)

    latest_time = (f.get("latest_time") or "").strip() or None
    if latest_time and latest_time <= preferred_time:
        latest_time = None  # ignore a window that ends before it starts

    partner_name = (f.get("partner_name") or "").strip() or None

    booking_id = db.add_booking(
        course=course, players=players, date=date_str,
        preferred_time=preferred_time, opens_at=opens_at.isoformat(),
        op_status=op["status"], op_message=op["message"],
        user_id=current_user.id,
        latest_time=latest_time,
        partner_name=partner_name,
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


# Per-user flag: "syncing" or "cancelling <ref>" while a site subprocess runs
_SITE_BUSY = {}


def _run_site_subprocess(user_id, args, label):
    """Run a Playwright subprocess for site sync/cancel in a background thread."""
    import subprocess, sys as _sys, threading, os as _os

    def _run():
        try:
            result = subprocess.run(
                [_sys.executable] + args,
                capture_output=True, text=True, timeout=600,
                cwd=_os.path.dirname(__file__),
            )
            if result.stdout:
                print(f"[{label} stdout]\n{result.stdout}", flush=True)
            if result.stderr:
                print(f"[{label} stderr]\n{result.stderr}", flush=True)
        finally:
            _SITE_BUSY.pop(user_id, None)

    threading.Thread(target=_run, daemon=True).start()


@app.route("/sync_site", methods=["POST"])
@login_required
def sync_site():
    """Refresh the dashboard's view of live Burhill bookings."""
    if not _SITE_BUSY.get(current_user.id):
        _SITE_BUSY[current_user.id] = "syncing"
        _run_site_subprocess(
            current_user.id,
            ["run_sync.py", "--user-id", str(current_user.id)],
            f"sync user {current_user.id}")
    return redirect(url_for("index"))


@app.route("/cancel_site/<ref>", methods=["POST"])
@login_required
def cancel_site(ref):
    """Cancel a live Burhill booking by its ESP ref (from the synced list)."""
    rows = db.get_site_bookings(current_user.id)
    if any(r["ref"] == ref for r in rows) and not _SITE_BUSY.get(current_user.id):
        _SITE_BUSY[current_user.id] = f"cancelling {ref}"
        _run_site_subprocess(
            current_user.id,
            ["run_cancel.py", "--ref", ref, "--user-id", str(current_user.id)],
            f"cancel ref {ref}")
    return redirect(url_for("index"))


@app.route("/api/verify_member", methods=["POST"])
@login_required
def verify_member():
    """Kick off a background member search on the Burhill site."""
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return {"error": "no name"}, 400
    if _SITE_BUSY.get(current_user.id):
        return {"error": "busy"}, 409
    db.set_member_search(current_user.id, name, "running")
    _SITE_BUSY[current_user.id] = "verifying"
    _run_site_subprocess(
        current_user.id,
        ["run_verify.py", "--user-id", str(current_user.id), "--query", name],
        f"verify member {name!r}")
    return {"status": "running"}


@app.route("/api/verify_member", methods=["GET"])
@login_required
def verify_member_status():
    row = db.get_member_search(current_user.id)
    if not row:
        return {"status": "none"}
    out = {"status": row["status"], "query": row["query"]}
    if row.get("results"):
        import json as _json
        try:
            out["results"] = _json.loads(row["results"])
        except Exception:
            out["results"] = {}
    return out


@app.route("/cancel_on_site/<int:booking_id>", methods=["POST"])
@login_required
def cancel_on_site(booking_id):
    """Cancel a booking that was actually made on the Burhill site."""
    b = db.get_booking(booking_id)
    if b and b["status"] == "booked" and (b["user_id"] == current_user.id or current_user.is_admin):
        db.update_status(booking_id, "cancelling", message="Cancelling on Burhill site …")
        import subprocess, sys as _sys, threading, os as _os

        def _run():
            script = _os.path.join(_os.path.dirname(__file__), "run_cancel.py")
            try:
                result = subprocess.run(
                    [_sys.executable, script, "--booking-id", str(booking_id)],
                    capture_output=True, text=True, timeout=600,
                )
            except subprocess.TimeoutExpired:
                db.update_status(booking_id, "booked",
                                 message="Cancel timed out — check the Burhill site")
                return
            if result.stdout:
                print(f"[cancel {booking_id} stdout]\n{result.stdout}", flush=True)
            if result.stderr:
                print(f"[cancel {booking_id} stderr]\n{result.stderr}", flush=True)

        threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/bookings/clear", methods=["POST"])
@login_required
def clear_finished():
    n = db.clear_finished_bookings(current_user.id)
    flash(f"Cleared {n} finished booking record(s).", "success")
    return redirect(url_for("index"))


@app.route("/delete/<int:booking_id>", methods=["POST"])
@login_required
def delete(booking_id):
    b = db.get_booking(booking_id)
    if b and (b["user_id"] == current_user.id or current_user.is_admin):
        sched.cancel_booking(booking_id)
        db.delete_booking(booking_id)
    return redirect(url_for("index"))


# ── Birdies ──────────────────────────────────────────────────────────────────

@app.route("/birdies")
@login_required
def birdies():
    season = str(datetime.now().year)
    all_birdies = db.get_birdies()
    season_counts = {}
    for b in all_birdies:
        if b["date"].endswith(f"/{season}"):
            season_counts[b["player_name"]] = season_counts.get(b["player_name"], 0) + 1
    games_counts = db.games_played_counts()
    names = set(season_counts) | set(games_counts)
    hcps = db.handicaps_by_name()
    merit = sorted(
        ({"name": n,
          "birdies": season_counts.get(n, 0),
          "games": games_counts.get(n, 0),
          "handicap": hcps.get(n, "")}
         for n in names),
        key=lambda r: (-r["birdies"], -r["games"], r["name"]))
    return render_template(
        "birdies.html",
        birdies=all_birdies,
        leaderboard=db.birdie_leaderboard(),
        merit=merit,
        season=season,
        player_names=db.get_player_names(),
        today=datetime.now().strftime("%Y-%m-%d"),
        user=current_user,
    )


@app.route("/birdies/add", methods=["POST"])
@login_required
def add_birdie():
    f = request.form
    player = (f.get("player_name") or "").strip() or current_user.name
    try:
        hole = int(f.get("hole", 0))
    except ValueError:
        hole = 0
    date_iso = f.get("date") or datetime.now().strftime("%Y-%m-%d")
    course = f.get("course")
    if course not in ("Old", "New"):
        course = None
    if 1 <= hole <= 18:
        date_str = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
        birdie_id = db.add_birdie(current_user.id, player, hole, date_str, course=course)
        db.add_player_name(player)  # remember the name for autocomplete
        photo = request.files.get("photo")
        if photo and photo.filename:
            try:
                fname = _save_birdie_photo(photo, birdie_id)
                db.update_birdie_photo(birdie_id, fname)
            except Exception as e:
                flash(f"Birdie logged, but the photo failed: {str(e)[:100]}", "error")
    return redirect(url_for("birdies"))


@app.route("/birdies/players/add", methods=["POST"])
@login_required
def add_player_name():
    name = (request.form.get("name") or "").strip()
    if name:
        db.add_player_name(name)
        flash(f"Added {name} to the player list.", "success")
    return redirect(url_for("birdies"))


@app.route("/birdies/players/delete", methods=["POST"])
@login_required
def delete_player_name():
    db.delete_player_name((request.form.get("name") or "").strip())
    return redirect(url_for("birdies"))


@app.route("/birdies/delete/<int:birdie_id>", methods=["POST"])
@login_required
def delete_birdie(birdie_id):
    b = db.get_birdie(birdie_id)
    if b and (b["user_id"] == current_user.id or current_user.is_admin):
        db.delete_birdie(birdie_id)
    return redirect(url_for("birdies"))


# ── Birdie photos ────────────────────────────────────────────────────────────

def _photo_dir() -> str:
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    path = os.path.join(base, "birdie_photos")
    os.makedirs(path, exist_ok=True)
    return path


def _save_birdie_photo(file_storage, birdie_id: int) -> str:
    """Resize and save an uploaded photo; returns the stored filename."""
    from PIL import Image, ImageOps
    img = Image.open(file_storage.stream)
    img = ImageOps.exif_transpose(img)  # respect phone camera orientation
    img.thumbnail((1200, 1200))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    fname = f"birdie_{birdie_id}.jpg"
    img.save(os.path.join(_photo_dir(), fname), "JPEG", quality=82)
    return fname


@app.route("/birdie_photo/<int:birdie_id>")
@login_required
def birdie_photo(birdie_id):
    from flask import send_from_directory, abort
    b = db.get_birdie(birdie_id)
    if not b or not b.get("photo"):
        abort(404)
    return send_from_directory(_photo_dir(), b["photo"])


# ── Games (who's playing board) ──────────────────────────────────────────────

GAMES_ENABLED = os.environ.get("GAMES_ENABLED", "0") == "1"


@app.route("/games")
@login_required
def games():
    if not GAMES_ENABLED:
        return redirect(url_for("index"))
    return render_template(
        "games.html",
        games=db.get_games(),
        today=datetime.now().strftime("%Y-%m-%d"),
        max_date=_max_booking_date().strftime("%Y-%m-%d"),
        user=current_user,
    )


@app.route("/games/add", methods=["POST"])
@login_required
def add_game():
    f = request.form
    date_iso = f.get("date") or ""
    try:
        datetime.strptime(date_iso, "%Y-%m-%d")
    except ValueError:
        flash("Pick a valid date.", "error")
        return redirect(url_for("games"))
    try:
        spaces = max(2, min(4, int(f.get("spaces", 4))))
    except ValueError:
        spaces = 4
    game_id = db.add_game(
        creator_id=current_user.id,
        date=date_iso,
        time_window=(f.get("time_window") or "").strip()[:30],
        course=f.get("course") or "Any",
        spaces=spaces,
        notes=(f.get("notes") or "").strip()[:200],
    )
    db.join_game(game_id, current_user.id)  # the creator is playing
    return redirect(url_for("games"))


@app.route("/games/join/<int:game_id>", methods=["POST"])
@login_required
def join_game(game_id):
    g = db.get_game(game_id)
    if g and len(g["players"]) < g["spaces"]:
        db.join_game(game_id, current_user.id)
    return redirect(url_for("games"))


@app.route("/games/leave/<int:game_id>", methods=["POST"])
@login_required
def leave_game(game_id):
    db.leave_game(game_id, current_user.id)
    return redirect(url_for("games"))


@app.route("/games/delete/<int:game_id>", methods=["POST"])
@login_required
def delete_game(game_id):
    g = db.get_game(game_id)
    if g and (g["creator_id"] == current_user.id or current_user.is_admin):
        db.delete_game(game_id)
    return redirect(url_for("games"))


@app.route("/games/book/<int:game_id>", methods=["POST"])
@login_required
def book_game(game_id):
    """Queue a tee-time booking for this game (creator only)."""
    g = db.get_game(game_id)
    if not g or g["creator_id"] != current_user.id:
        return redirect(url_for("games"))

    dt = datetime.strptime(g["date"], "%Y-%m-%d")
    if dt > _max_booking_date():
        flash("That date is beyond the known open play schedule — can't book yet.",
              "error")
        return redirect(url_for("games"))

    import re as _re
    window = g.get("time_window") or ""
    parts = [p.strip() for p in window.split("-")]
    def _valid(t):
        return bool(_re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t or ""))
    preferred = parts[0] if parts and _valid(parts[0]) else "09:00"
    latest = parts[1] if len(parts) > 1 and _valid(parts[1]) else None
    date_str = dt.strftime("%d/%m/%Y")
    course = g.get("course") or "Golf"
    if course == "Any":
        course = "Golf"
    op = check_booking(date_str, course, preferred)
    open_dt = dt - timedelta(days=BOOKING_WINDOW["days_in_advance"])
    h, m = map(int, BOOKING_WINDOW["open_time"].split(":"))
    opens_at = open_dt.replace(hour=h, minute=m, second=0, microsecond=0)

    booking_id = db.add_booking(
        course=course, players=len(g["players"]), date=date_str,
        preferred_time=preferred, opens_at=opens_at.isoformat(),
        op_status=op["status"], op_message=op["message"],
        user_id=current_user.id, latest_time=latest,
    )
    sched.schedule_booking(booking_id, opens_at)
    flash(f"Tee time queued for {date_str} ({len(g['players'])} players).", "success")
    return redirect(url_for("games"))


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
