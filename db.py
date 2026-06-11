"""
Database layer — works with SQLite locally and Postgres on Railway.
Reads DATABASE_URL env var when present (Railway injects this automatically
when you add a Postgres plugin). Falls back to local SQLite otherwise.
"""
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, text

# Railway provides postgres:// but SQLAlchemy needs postgresql://
def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url.replace("postgres://", "postgresql://", 1)
    path = os.path.join(os.path.dirname(__file__), "bookings.db")
    return f"sqlite:///{path}"

# Shared engine (thread-safe)
_engine = None

def get_engine():
    global _engine
    if _engine is None:
        url = _db_url()
        kwargs = {} if url.startswith("sqlite") else {"pool_pre_ping": True}
        _engine = create_engine(url, **kwargs)
    return _engine


# SQLite uses ? placeholders; Postgres uses :name params — use named style throughout
_CREATE = """
CREATE TABLE IF NOT EXISTS bookings (
    id                SERIAL PRIMARY KEY,
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
"""

# SQLite doesn't support SERIAL — use INTEGER PRIMARY KEY AUTOINCREMENT
_CREATE_SQLITE = """
CREATE TABLE IF NOT EXISTS bookings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
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
"""


def init_db():
    engine = get_engine()
    sql = _CREATE_SQLITE if _db_url().startswith("sqlite") else _CREATE
    with engine.begin() as conn:
        conn.execute(text(sql))


def add_booking(course, players, date, preferred_time,
                opens_at, op_status, op_message) -> int:
    engine = get_engine()
    is_pg = not _db_url().startswith("sqlite")
    sql = """
        INSERT INTO bookings
            (course, players, date, preferred_time, status, opens_at,
             created_at, open_play_status, open_play_message)
        VALUES
            (:course, :players, :date, :preferred_time, 'pending', :opens_at,
             :created_at, :op_status, :op_message)
    """
    if is_pg:
        sql += " RETURNING id"
    params = dict(
        course=course, players=players, date=date,
        preferred_time=preferred_time, opens_at=opens_at,
        created_at=datetime.now().isoformat(),
        op_status=op_status, op_message=op_message,
    )
    with engine.begin() as conn:
        result = conn.execute(text(sql), params)
        if is_pg:
            return result.fetchone()[0]
        return result.lastrowid


def get_booking(booking_id: int) -> Optional[dict]:
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM bookings WHERE id = :id"), {"id": booking_id}
        ).mappings().fetchone()
        return dict(row) if row else None


def get_all_bookings() -> list:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM bookings ORDER BY date, preferred_time")
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
