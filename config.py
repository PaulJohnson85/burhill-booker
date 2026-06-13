# ── Burhill Golf Club Tee Time Booker ──────────────────────────────────────
# Credentials are read from environment variables so they're never stored in code.
# Set them in your shell, a .env file, or Railway's environment variable panel.
#
#   BURHILL_USERNAME=PaulJohnson1
#   BURHILL_PASSWORD=yourpassword
#
import os

CREDENTIALS = {
    "username": os.environ.get("BURHILL_USERNAME", ""),
    "password": os.environ.get("BURHILL_PASSWORD", ""),
}

BOOKING = {
    # Which course: "Golf" (any), "Old" (Old Course), "New" (New Course)
    "course": "Golf",

    # Number of players (1–4)
    "players": 2,

    # Target date in DD/MM/YYYY format
    "date": "17/06/2026",

    # Preferred tee time as HH:MM (24h). The bot books the FIRST slot at or
    # after this time. Set to "07:00" to grab the earliest available.
    "preferred_time": "09:00",

    # How many minutes before bookings open to start the browser (safety buffer).
    # The bot logs in and sits on the calendar, refreshing until the date opens.
    "lead_time_minutes": 1,
}

# When does Burhill open bookings for a given day?
BOOKING_WINDOW = {
    "days_in_advance": 7,   # bookings open X days before the round
    "open_time": "07:00",   # HH:MM local time when the booking window opens
}
