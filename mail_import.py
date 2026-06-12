"""
Import open play PDFs that Burhill emails to the user's mailbox.

Polls the inbox over IMAP (scheduled every 6 hours), finds messages from
Burhill carrying a PDF attachment, parses the open play schedule and stores
it in the database. Processed emails are remembered by Message-ID so nothing
is imported twice.

Environment variables:
    IMAP_HOST  — default imap.gmail.com
    IMAP_USER  — mailbox address
    IMAP_PASS  — app password (Gmail: Security → App passwords)
    IMAP_FROM  — sender filter substring, default "burhill"
"""
import email
import email.utils
import imaplib
import os
import tempfile
from datetime import datetime


def _p(msg):
    print(f"[mail_import] {msg}", flush=True)


def _infer_year(schedule_month: int, email_dt: datetime) -> int:
    """The PDF covers a month; pick the year so it lands within ~6 months
    after the email (Dec email about January = next year)."""
    year = email_dt.year
    if schedule_month < email_dt.month - 6:
        year += 1
    return year


def _import_pdf(pdf_bytes: bytes, email_dt: datetime) -> int:
    """Parse the PDF and store its schedule in the DB. Returns days stored."""
    import db
    from open_play import parse_pdf

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        path = tmp.name
    try:
        # Parse with the email's year first, then correct if the PDF's month
        # implies it is actually for early next year.
        schedule = parse_pdf(path, email_dt.year)
        if not schedule:
            return 0
        first_key = next(iter(schedule))
        month = int(first_key.split("/")[1])
        year = _infer_year(month, email_dt)
        if year != email_dt.year:
            schedule = parse_pdf(path, year)
        return db.upsert_open_play(schedule)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def check_mail() -> int:
    """Poll the inbox; import any unprocessed Burhill open play PDFs.
    Returns the number of PDFs imported."""
    import db

    user = os.environ.get("IMAP_USER")
    password = os.environ.get("IMAP_PASS")
    if not user or not password:
        _p("IMAP_USER/IMAP_PASS not set — skipping mailbox check")
        return 0
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    sender = os.environ.get("IMAP_FROM", "burhill")

    imported = 0
    conn = imaplib.IMAP4_SSL(host)
    try:
        conn.login(user, password)
        conn.select("INBOX", readonly=True)
        status, data = conn.search(None, "FROM", f'"{sender}"')
        if status != "OK":
            _p(f"search failed: {status}")
            return 0
        ids = data[0].split()
        _p(f"{len(ids)} message(s) from '{sender}'")

        for num in ids[-30:]:  # most recent 30 is plenty
            status, msg_data = conn.fetch(num, "(RFC822)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            message_id = msg.get("Message-ID", "").strip()
            if not message_id or db.email_already_processed(message_id):
                continue

            email_dt = datetime.now()
            try:
                email_dt = email.utils.parsedate_to_datetime(msg.get("Date"))
            except Exception:
                pass

            pdfs = []
            for part in msg.walk():
                fname = part.get_filename() or ""
                ctype = part.get_content_type()
                if fname.lower().endswith(".pdf") or ctype == "application/pdf":
                    payload = part.get_payload(decode=True)
                    if payload:
                        pdfs.append((fname, payload))

            if not pdfs:
                db.mark_email_processed(message_id)
                continue

            _p(f"importing {len(pdfs)} PDF(s) from '{msg.get('Subject', '')[:60]}' "
               f"({email_dt:%d/%m/%Y})")
            ok = True
            for fname, payload in pdfs:
                try:
                    days = _import_pdf(payload, email_dt)
                    _p(f"  {fname}: {days} day(s) imported")
                    if days:
                        imported += 1
                except Exception as e:
                    ok = False
                    _p(f"  {fname}: import failed: {e}")
            if ok:
                db.mark_email_processed(message_id)
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    _p(f"done — {imported} PDF(s) imported")
    return imported


if __name__ == "__main__":
    check_mail()
