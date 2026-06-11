"""SQLite persistence for the booking queue."""
import sqlite3
import os
from datetime import datetime
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), 'bookings.db')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    course           TEXT    NOT NULL,
    players          INTEGER NOT NULL,
    date             TEXT    NOT NULL,
    preferred_time   TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'pending',
    opens_at         TEXT,
    created_at       TEXT    NOT NULL,
    booked_at        TEXT,
    slot_time        TEXT,
    message          TEXT,
    open_play_status  TEXT,
    open_play_message TEXT
);
"""


def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.executescript(_SCHEMA)


def add_booking(course, players, date, preferred_time,
                opens_at, op_status, op_message) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO bookings
               (course, players, date, preferred_time, status, opens_at,
                created_at, open_play_status, open_play_message)
               VALUES (?,?,?,?,'pending',?,?,?,?)""",
            (course, players, date, preferred_time,
             opens_at, datetime.now().isoformat(), op_status, op_message),
        )
        return cur.lastrowid


def get_booking(booking_id: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        return dict(row) if row else None


def get_all_bookings() -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM bookings ORDER BY date, preferred_time"
        ).fetchall()
        return [dict(r) for r in rows]


def update_status(booking_id: int, status: str, *,
                  message: str = None,
                  slot_time: str = None,
                  booked_at: str = None):
    with _conn() as c:
        c.execute(
            """UPDATE bookings SET
               status    = ?,
               message   = COALESCE(?, message),
               slot_time = COALESCE(?, slot_time),
               booked_at = COALESCE(?, booked_at)
               WHERE id = ?""",
            (status, message, slot_time, booked_at, booking_id),
        )


def delete_booking(booking_id: int):
    with _conn() as c:
        c.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
