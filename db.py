"""
Database layer — works with SQLite locally and Postgres on Railway.
Reads DATABASE_URL env var when present (Railway injects this automatically
when you add a Postgres plugin). Falls back to local SQLite otherwise.
"""
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, text


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url.replace("postgres://", "postgresql://", 1)
    path = os.path.join(os.path.dirname(__file__), "bookings.db")
    return f"sqlite:///{path}"


_engine = None

def get_engine():
    global _engine
    if _engine is None:
        url = _db_url()
        kwargs = {} if url.startswith("sqlite") else {"pool_pre_ping": True}
        _engine = create_engine(url, **kwargs)
    return _engine


def _is_pg() -> bool:
    return not _db_url().startswith("sqlite")


def init_db():
    engine = get_engine()
    pg = _is_pg()
    id_col = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    bool_t = "BOOLEAN" if pg else "INTEGER"

    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS users (
                id            {id_col},
                name          TEXT NOT NULL,
                email         TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                burhill_user  TEXT,
                burhill_pass  TEXT,
                is_admin      {bool_t} NOT NULL DEFAULT {'FALSE' if pg else '0'},
                created_at    TEXT NOT NULL
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS bookings (
                id                {id_col},
                user_id           INTEGER REFERENCES users(id),
                course            TEXT    NOT NULL,
                players           INTEGER NOT NULL,
                date              TEXT    NOT NULL,
                preferred_time    TEXT    NOT NULL,
                status            TEXT    NOT NULL DEFAULT 'pending',
                opens_at          TEXT,
                created_at        TEXT    NOT NULL,
                booked_at         TEXT,
                slot_time         TEXT,
                message           TEXT,
                open_play_status  TEXT,
                open_play_message TEXT
            )
        """))

    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS site_bookings (
                id           {id_col},
                user_id      INTEGER REFERENCES users(id),
                ref          TEXT,
                date_text    TEXT,
                course       TEXT,
                participants TEXT,
                raw          TEXT,
                can_cancel   {bool_t} NOT NULL DEFAULT {'FALSE' if pg else '0'},
                synced_at    TEXT
            )
        """))

    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS member_search (
                id         {id_col},
                user_id    INTEGER REFERENCES users(id),
                query      TEXT,
                status     TEXT,
                results    TEXT,
                updated_at TEXT
            )
        """))

    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS birdies (
                id          {id_col},
                user_id     INTEGER REFERENCES users(id),
                player_name TEXT NOT NULL,
                course      TEXT,
                hole        INTEGER NOT NULL,
                date        TEXT NOT NULL,
                photo       TEXT,
                created_at  TEXT NOT NULL
            )
        """))

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS open_play_days (
                date_key   TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                updated_at TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                message_id   TEXT PRIMARY KEY,
                processed_at TEXT
            )
        """))

    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS games (
                id          {id_col},
                creator_id  INTEGER REFERENCES users(id),
                date        TEXT NOT NULL,
                time_window TEXT,
                course      TEXT,
                spaces      INTEGER NOT NULL DEFAULT 4,
                notes       TEXT,
                status      TEXT NOT NULL DEFAULT 'open',
                created_at  TEXT NOT NULL
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS game_players (
                id        {id_col},
                game_id   INTEGER REFERENCES games(id),
                user_id   INTEGER REFERENCES users(id),
                joined_at TEXT NOT NULL
            )
        """))

    # Separate transactions — an ALTER TABLE failure must not roll back the rest
    for ddl in ("ALTER TABLE bookings ADD COLUMN user_id INTEGER",
                "ALTER TABLE bookings ADD COLUMN latest_time TEXT",
                "ALTER TABLE bookings ADD COLUMN partner_name TEXT",
                "ALTER TABLE birdies ADD COLUMN course TEXT"):
        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
        except Exception:
            pass  # column already exists


# ── Users ───────────────────────────────────────────────────────────────────

def create_user(name: str, email: str, password_hash: str,
                burhill_user: str, burhill_pass_encrypted: str) -> int:
    pg = _is_pg()
    sql = """
        INSERT INTO users (name, email, password_hash, burhill_user, burhill_pass, created_at)
        VALUES (:name, :email, :password_hash, :burhill_user, :burhill_pass, :created_at)
    """
    if pg:
        sql += " RETURNING id"
    params = dict(name=name, email=email, password_hash=password_hash,
                  burhill_user=burhill_user, burhill_pass=burhill_pass_encrypted,
                  created_at=datetime.now().isoformat())
    with get_engine().begin() as conn:
        result = conn.execute(text(sql), params)
        return result.fetchone()[0] if pg else result.lastrowid


def get_user_by_id(user_id: int) -> Optional[dict]:
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM users WHERE id = :id"), {"id": user_id}
        ).mappings().fetchone()
        return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[dict]:
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM users WHERE email = :email"), {"email": email}
        ).mappings().fetchone()
        return dict(row) if row else None


def update_user_credentials(user_id: int, burhill_user: str, burhill_pass_encrypted: str):
    with get_engine().begin() as conn:
        conn.execute(text("""
            UPDATE users SET burhill_user = :bu, burhill_pass = :bp WHERE id = :id
        """), {"bu": burhill_user, "bp": burhill_pass_encrypted, "id": user_id})


def get_all_users() -> list:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT id, name, email, is_admin, created_at FROM users ORDER BY created_at")
        ).mappings().fetchall()
        return [dict(r) for r in rows]


# ── Bookings ─────────────────────────────────────────────────────────────────

def add_booking(course, players, date, preferred_time,
                opens_at, op_status, op_message, user_id=None,
                latest_time=None, partner_name=None) -> int:
    pg = _is_pg()
    sql = """
        INSERT INTO bookings
            (user_id, course, players, date, preferred_time, latest_time,
             partner_name, status, opens_at, created_at,
             open_play_status, open_play_message)
        VALUES
            (:user_id, :course, :players, :date, :preferred_time, :latest_time,
             :partner_name, 'pending', :opens_at, :created_at,
             :op_status, :op_message)
    """
    if pg:
        sql += " RETURNING id"
    params = dict(user_id=user_id, course=course, players=players, date=date,
                  preferred_time=preferred_time, latest_time=latest_time,
                  partner_name=partner_name,
                  opens_at=opens_at, created_at=datetime.now().isoformat(),
                  op_status=op_status, op_message=op_message)
    with get_engine().begin() as conn:
        result = conn.execute(text(sql), params)
        return result.fetchone()[0] if pg else result.lastrowid


def get_booking(booking_id: int) -> Optional[dict]:
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM bookings WHERE id = :id"), {"id": booking_id}
        ).mappings().fetchone()
        return dict(row) if row else None


def get_bookings_for_user(user_id: int) -> list:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM bookings WHERE user_id = :uid ORDER BY date, preferred_time"),
            {"uid": user_id}
        ).mappings().fetchall()
        return [dict(r) for r in rows]


def get_all_bookings() -> list:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("""
                SELECT b.*, u.name as user_name, u.email as user_email
                FROM bookings b
                LEFT JOIN users u ON b.user_id = u.id
                ORDER BY b.date, b.preferred_time
            """)
        ).mappings().fetchall()
        return [dict(r) for r in rows]


# ── Games (who's playing board) ──────────────────────────────────────────────

def add_game(creator_id: int, date: str, time_window: str, course: str,
             spaces: int, notes: str = "") -> int:
    pg = _is_pg()
    sql = """
        INSERT INTO games (creator_id, date, time_window, course, spaces, notes,
                           status, created_at)
        VALUES (:creator_id, :date, :time_window, :course, :spaces, :notes,
                'open', :created_at)
    """
    if pg:
        sql += " RETURNING id"
    params = dict(creator_id=creator_id, date=date, time_window=time_window,
                  course=course, spaces=spaces, notes=notes,
                  created_at=datetime.now().isoformat())
    with get_engine().begin() as conn:
        result = conn.execute(text(sql), params)
        return result.fetchone()[0] if pg else result.lastrowid


def get_games(include_past: bool = False) -> list:
    with get_engine().connect() as conn:
        games = [dict(r) for r in conn.execute(text("""
            SELECT g.*, u.name as creator_name
            FROM games g LEFT JOIN users u ON g.creator_id = u.id
            ORDER BY g.date, g.time_window
        """)).mappings().fetchall()]
        players = [dict(r) for r in conn.execute(text("""
            SELECT gp.*, u.name as user_name
            FROM game_players gp LEFT JOIN users u ON gp.user_id = u.id
            ORDER BY gp.joined_at
        """)).mappings().fetchall()]
    by_game = {}
    for p in players:
        by_game.setdefault(p["game_id"], []).append(p)
    out = []
    today = datetime.now().strftime("%Y-%m-%d")
    for g in games:
        # date stored as YYYY-MM-DD for sorting
        if not include_past and g["date"] < today:
            continue
        g["players"] = by_game.get(g["id"], [])
        out.append(g)
    return out


def get_game(game_id: int):
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT * FROM games WHERE id = :id"),
                           {"id": game_id}).mappings().fetchone()
        if not row:
            return None
        g = dict(row)
        g["players"] = [dict(r) for r in conn.execute(text("""
            SELECT gp.*, u.name as user_name
            FROM game_players gp LEFT JOIN users u ON gp.user_id = u.id
            WHERE gp.game_id = :id ORDER BY gp.joined_at
        """), {"id": game_id}).mappings().fetchall()]
        return g


def join_game(game_id: int, user_id: int):
    with get_engine().begin() as conn:
        existing = conn.execute(text("""
            SELECT 1 FROM game_players WHERE game_id = :g AND user_id = :u
        """), {"g": game_id, "u": user_id}).fetchone()
        if existing:
            return
        conn.execute(text("""
            INSERT INTO game_players (game_id, user_id, joined_at)
            VALUES (:g, :u, :ts)
        """), {"g": game_id, "u": user_id, "ts": datetime.now().isoformat()})


def leave_game(game_id: int, user_id: int):
    with get_engine().begin() as conn:
        conn.execute(text("""
            DELETE FROM game_players WHERE game_id = :g AND user_id = :u
        """), {"g": game_id, "u": user_id})


def delete_game(game_id: int):
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM game_players WHERE game_id = :id"),
                     {"id": game_id})
        conn.execute(text("DELETE FROM games WHERE id = :id"), {"id": game_id})


def games_played_counts() -> dict:
    """user name → number of past/today games they joined."""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT u.name as name, COUNT(*) as total
            FROM game_players gp
            JOIN games g ON gp.game_id = g.id
            LEFT JOIN users u ON gp.user_id = u.id
            WHERE g.date <= :today
            GROUP BY u.name
        """), {"today": today}).mappings().fetchall()
        return {r["name"]: r["total"] for r in rows if r["name"]}


def update_birdie_photo(birdie_id: int, photo: str):
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE birdies SET photo = :p WHERE id = :id"),
                     {"p": photo, "id": birdie_id})


# ── Open play schedule (imported via upload or email) ───────────────────────

def upsert_open_play(schedule: dict) -> int:
    """Store/overwrite open play day entries. schedule: {DD/MM/YYYY: {...}}."""
    import json as _json
    with get_engine().begin() as conn:
        for date_key, info in schedule.items():
            conn.execute(text("DELETE FROM open_play_days WHERE date_key = :k"),
                         {"k": date_key})
            conn.execute(text("""
                INSERT INTO open_play_days (date_key, data, updated_at)
                VALUES (:k, :data, :ts)
            """), {"k": date_key, "data": _json.dumps(info),
                   "ts": datetime.now().isoformat()})
    return len(schedule)


def get_open_play_all() -> dict:
    import json as _json
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT date_key, data FROM open_play_days")).fetchall()
    out = {}
    for k, data in rows:
        try:
            out[k] = _json.loads(data)
        except Exception:
            continue
    return out


def email_already_processed(message_id: str) -> bool:
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM processed_emails WHERE message_id = :m"),
            {"m": message_id}).fetchone()
        return row is not None


def mark_email_processed(message_id: str):
    with get_engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO processed_emails (message_id, processed_at)
            VALUES (:m, :ts)
        """), {"m": message_id, "ts": datetime.now().isoformat()})


# ── Birdies ─────────────────────────────────────────────────────────────────

def add_birdie(user_id: int, player_name: str, hole: int, date: str,
               course: str = None) -> int:
    pg = _is_pg()
    sql = """
        INSERT INTO birdies (user_id, player_name, course, hole, date, created_at)
        VALUES (:user_id, :player_name, :course, :hole, :date, :created_at)
    """
    if pg:
        sql += " RETURNING id"
    params = dict(user_id=user_id, player_name=player_name, course=course,
                  hole=hole, date=date, created_at=datetime.now().isoformat())
    with get_engine().begin() as conn:
        result = conn.execute(text(sql), params)
        return result.fetchone()[0] if pg else result.lastrowid


def get_birdies() -> list:
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT b.*, u.name as logged_by
            FROM birdies b LEFT JOIN users u ON b.user_id = u.id
            ORDER BY b.date DESC, b.created_at DESC
        """)).mappings().fetchall()
        return [dict(r) for r in rows]


def get_birdie(birdie_id: int) -> Optional[dict]:
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT * FROM birdies WHERE id = :id"),
                           {"id": birdie_id}).mappings().fetchone()
        return dict(row) if row else None


def delete_birdie(birdie_id: int):
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM birdies WHERE id = :id"), {"id": birdie_id})


def birdie_leaderboard() -> list:
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT player_name, COUNT(*) as total
            FROM birdies GROUP BY player_name ORDER BY total DESC, player_name
        """)).mappings().fetchall()
        return [dict(r) for r in rows]


# ── Member search (verify playing partner) ─────────────────────────────────

def set_member_search(user_id: int, query: str, status: str, results: str = None):
    """One row per user — replace any previous search."""
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM member_search WHERE user_id = :uid"),
                     {"uid": user_id})
        conn.execute(text("""
            INSERT INTO member_search (user_id, query, status, results, updated_at)
            VALUES (:uid, :query, :status, :results, :updated_at)
        """), dict(uid=user_id, query=query, status=status, results=results,
                   updated_at=datetime.now().isoformat()))


def get_member_search(user_id: int) -> Optional[dict]:
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM member_search WHERE user_id = :uid"),
            {"uid": user_id}
        ).mappings().fetchone()
        return dict(row) if row else None


# ── Site bookings (synced from Burhill's book_history.php) ─────────────────

def replace_site_bookings(user_id: int, rows: list):
    """Replace the user's synced site bookings with a fresh set.
    Each row: dict(ref, date_text, course, participants, raw, can_cancel)."""
    now = datetime.now().isoformat()
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM site_bookings WHERE user_id = :uid"),
                     {"uid": user_id})
        for r in rows:
            conn.execute(text("""
                INSERT INTO site_bookings
                    (user_id, ref, date_text, course, participants, raw, can_cancel, synced_at)
                VALUES (:uid, :ref, :date_text, :course, :participants, :raw, :can_cancel, :synced_at)
            """), dict(uid=user_id, ref=r.get("ref"), date_text=r.get("date_text"),
                       course=r.get("course"), participants=r.get("participants"),
                       raw=r.get("raw"), can_cancel=bool(r.get("can_cancel")),
                       synced_at=now))


def get_site_bookings(user_id: int) -> list:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM site_bookings WHERE user_id = :uid ORDER BY id"),
            {"uid": user_id}
        ).mappings().fetchall()
        return [dict(r) for r in rows]


def delete_site_booking(user_id: int, ref: str):
    with get_engine().begin() as conn:
        conn.execute(text(
            "DELETE FROM site_bookings WHERE user_id = :uid AND ref = :ref"),
            {"uid": user_id, "ref": ref})


def update_status(booking_id: int, status: str, *,
                  message: str = None,
                  slot_time: str = None,
                  booked_at: str = None):
    with get_engine().begin() as conn:
        conn.execute(text("""
            UPDATE bookings SET
                status    = :status,
                message   = COALESCE(:message,   message),
                slot_time = COALESCE(:slot_time, slot_time),
                booked_at = COALESCE(:booked_at, booked_at)
            WHERE id = :id
        """), dict(status=status, message=message,
                   slot_time=slot_time, booked_at=booked_at, id=booking_id))


def delete_bookings_by_status(status: str) -> int:
    """Delete all bookings with the given status. Returns the row count."""
    with get_engine().begin() as conn:
        result = conn.execute(
            text("DELETE FROM bookings WHERE status = :status"), {"status": status})
        return result.rowcount or 0


def delete_booking(booking_id: int):
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM bookings WHERE id = :id"), {"id": booking_id})
