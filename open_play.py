"""
Open play schedule loader and query helpers.

PDFs are parsed and stored as JSON in open_play_data/.
Each file covers one calendar month and is keyed by DD/MM (year-agnostic).
"""

import json
import os
import re
from datetime import datetime
from typing import Optional

DATA_DIR = os.path.join(os.path.dirname(__file__), "open_play_data")
os.makedirs(DATA_DIR, exist_ok=True)

# ── PDF parser ─────────────────────────────────────────────────────────────

def parse_pdf(path: str, year: int) -> dict:
    """
    Parse a Burhill open play PDF and return a dict keyed by DD/MM/YYYY.
    Saves the result to open_play_data/<YYYY-MM>.json automatically.
    """
    try:
        import pdfplumber
    except ImportError:
        raise ImportError("pdfplumber is required: pip3 install pdfplumber")

    schedule = {}
    month_key = None

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
            table = tables[0]
            # Skip title + header rows
            for row in table:
                if not row or not row[0] or '/' not in str(row[0]):
                    continue
                date_raw, day, tee_course, open_course, two_ball, op_times, event = (
                    row + [''] * 7
                )[:7]
                date_raw = (date_raw or '').strip()
                if not re.match(r'\d{2}/\d{2}/\d{4}', date_raw):
                    continue

                d, m, _y = date_raw.split('/')
                date_key = f"{d}/{m}/{year}"
                if month_key is None:
                    month_key = f"{year}-{m}"

                op_times = (op_times or '').strip()
                event    = (event or '').strip()
                tee      = (tee_course or '').strip() or None
                op       = (open_course or '').strip() or None

                # Parse open play end time from "HH:MM - HH:MM" or "All Day"
                end_time = None
                if op_times and op_times != 'All Day':
                    raw_end = re.split(r'[-–]', op_times.replace(' to ', '-'))[-1]
                    raw_end = raw_end.strip().lower().replace('pm', '').replace('am', '').strip()
                    try:
                        h, mn = map(int, raw_end.split(':'))
                        end_time = f"{h:02d}:{mn:02d}"
                    except Exception:
                        pass
                elif op_times == 'All Day':
                    end_time = '23:59'

                schedule[date_key] = {
                    'day': (day or '').strip(),
                    'tee_time_course': tee,
                    'open_play_course': op,
                    'open_play_times': op_times,
                    'open_play_ends': end_time,
                    'event': event,
                }

    if month_key and schedule:
        out_path = os.path.join(DATA_DIR, f"{month_key}.json")
        with open(out_path, 'w') as f:
            json.dump(schedule, f, indent=2)
        print(f"Saved {len(schedule)} days to {out_path}")

    return schedule


# ── Runtime lookup ─────────────────────────────────────────────────────────

def _load_all() -> dict:
    """Load every saved monthly JSON into a single dict keyed by DD/MM/YYYY."""
    combined = {}
    if os.path.isdir(DATA_DIR):
        for fname in os.listdir(DATA_DIR):
            if fname.endswith('.json'):
                with open(os.path.join(DATA_DIR, fname)) as f:
                    combined.update(json.load(f))
    return combined


def get_day_info(date_str: str) -> Optional[dict]:
    """
    Look up open play info for a given date (DD/MM/YYYY).
    Returns None if no data is available for that date.
    """
    all_data = _load_all()
    return all_data.get(date_str)


def check_booking(date_str: str, course: str, preferred_time: str) -> dict:
    """
    Check whether a booking is affected by open play.

    Returns a dict with:
      - status: 'ok' | 'open_play_course' | 'during_open_play' | 'no_data'
      - message: human-readable explanation
      - day_info: the raw schedule entry (or None)
    """
    info = get_day_info(date_str)
    if info is None:
        return {'status': 'no_data', 'message': 'No open play data for this date.', 'day_info': None}

    op_course = (info.get('open_play_course') or '').strip()
    tee_course = (info.get('tee_time_course') or '').strip()

    # Is the chosen course the open play course today?
    if op_course.lower() in course.lower() or course.lower() in op_course.lower():
        # Is the preferred time during open play?
        ends = info.get('open_play_ends')
        op_times = info.get('open_play_times', '')
        if op_times == 'All Day':
            return {
                'status': 'during_open_play',
                'message': f"{course} Course is open play ALL DAY on {date_str}. No member tee times available.",
                'day_info': info,
            }
        if ends:
            pref_h, pref_m = map(int, preferred_time.split(':'))
            end_h, end_m = map(int, ends.split(':'))
            pref_mins = pref_h * 60 + pref_m
            end_mins = end_h * 60 + end_m
            if pref_mins < end_mins:
                return {
                    'status': 'during_open_play',
                    'message': (
                        f"{course} Course is open play from {op_times} on {date_str} ({info['day']}). "
                        f"Your preferred time {preferred_time} falls within the open play window. "
                        f"First available slot after open play: ~{ends}."
                    ),
                    'day_info': info,
                }
        return {
            'status': 'open_play_course',
            'message': (
                f"{course} Course is the open play course on {date_str} ({info['day']}), "
                f"open play runs {op_times}."
            ),
            'day_info': info,
        }

    op_times = info.get('open_play_times', '')
    # "Golf" is the portal's "Any course" choice — name the real tee time
    # course from the schedule instead of saying "Golf Course"
    if course.strip().lower() in ('golf', 'any'):
        subject = (f"Tee times are on the {tee_course} Course" if tee_course
                   else "Tee times are available")
    else:
        subject = f"{course} Course is the tee time course"
    return {
        'status': 'ok',
        'message': (
            f"{subject} on {date_str} ({info['day']}). "
            f"Open play is on {op_course} ({op_times})."
        ),
        'day_info': info,
    }


# ── CLI: import a PDF ──────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print("Usage: python3 open_play.py <path_to_pdf> <year>")
        print("  e.g. python3 open_play.py open_play_july.pdf 2026")
        sys.exit(1)
    pdf_path = sys.argv[1]
    year = int(sys.argv[2])
    result = parse_pdf(pdf_path, year)
    print(f"Parsed {len(result)} entries.")
    for k, v in list(result.items())[:3]:
        print(f"  {k}: {v}")
