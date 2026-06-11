"""
Email notifications for booking confirmations and failures.

Required environment variables:
    NOTIFY_EMAIL   — address to send notifications TO (your email)
    SMTP_HOST      — e.g. smtp.gmail.com
    SMTP_PORT      — e.g. 587
    SMTP_USER      — your sending email address
    SMTP_PASS      — your SMTP password or Gmail app password

If any of these are missing, notifications are silently skipped.
"""
import os
import smtplib
import traceback
from email.message import EmailMessage


def _cfg():
    return {
        "to":   os.environ.get("NOTIFY_EMAIL", ""),
        "host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": os.environ.get("SMTP_USER", ""),
        "pwd":  os.environ.get("SMTP_PASS", ""),
    }


def _send(subject: str, body: str):
    cfg = _cfg()
    if not all([cfg["to"], cfg["user"], cfg["pwd"]]):
        return  # not configured — skip silently
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = cfg["user"]
        msg["To"]      = cfg["to"]
        msg.set_content(body)
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as s:
            s.starttls()
            s.login(cfg["user"], cfg["pwd"])
            s.send_message(msg)
        print(f"📧 Email sent: {subject}")
    except Exception:
        print(f"⚠️  Email failed (non-fatal):\n{traceback.format_exc()}")


def booking_confirmed(booking: dict, slot_time: str):
    course = booking["course"] if booking["course"] != "Golf" else "Any"
    _send(
        subject=f"✅ Tee time booked — {booking['date']} at {slot_time}",
        body=f"""Your tee time has been booked!

Date:     {booking['date']}
Time:     {slot_time}
Course:   {course}
Players:  {booking['players']}

The Burhill Booker got there automatically at 07:00. Enjoy your round! ⛳
""",
    )


def booking_failed(booking: dict, reason: str):
    course = booking["course"] if booking["course"] != "Golf" else "Any"
    _send(
        subject=f"❌ Booking failed — {booking['date']}",
        body=f"""Unfortunately your tee time booking failed.

Date:     {booking['date']}
Time:     {booking['preferred_time']}
Course:   {course}
Players:  {booking['players']}

Reason: {reason}

Please log in to the Burhill portal to book manually.
""",
    )
