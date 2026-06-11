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
        # Add user_id column to existing deployments that pre-date auth
        try:
            conn.execute(text("ALTER TABLE bookings ADD COLUMN user_id INTEGER"))
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
                opens_at, op_status, op_message, user_id=None) -> int:
    pg = _is_pg()
    sql = """
        INSERT INTO bookings
            (user_id, course, players, date, preferred_time, status, opens_at,
             created_at, open_play_status, open_play_message)
        VALUES
            (:user_id, :course, :players, :date, :preferred_time, 'pending', :opens_at,
             :created_at, :op_status, :op_message)
    """
    if pg:
        sql += " RETURNING id"
    params = dict(user_id=user_id, course=course, players=players, date=date,
                  preferred_time=preferred_time, opens_at=opens_at,
                  created_at=datetime.now().isoformat(),
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


def delete_booking(booking_id: int):
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM bookings WHERE id = :id"), {"id": booking_id})
