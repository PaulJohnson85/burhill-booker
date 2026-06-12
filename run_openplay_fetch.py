"""
Fetch the open play PDF from the Burhill members' website and import it.

Logs into https://members.burhillgolf-club.co.uk/, finds the open play
calendar PDF link(s), downloads and parses them, and stores the schedule
in the database. Runs on a schedule; can also be run manually:

    python3 run_openplay_fetch.py

Environment variables:
    MEMBERS_URL   — default https://members.burhillgolf-club.co.uk/
    MEMBERS_USER  — members site username
    MEMBERS_PASS  — members site password
"""
import os
import re
import tempfile
from datetime import datetime

from playwright.sync_api import sync_playwright


def _p(msg):
    print(f"[openplay_fetch] {msg}", flush=True)


def _infer_year(schedule_month: int, now: datetime) -> int:
    year = now.year
    if schedule_month < now.month - 6:
        year += 1
    return year


def _import_pdf_bytes(pdf_bytes: bytes, label: str) -> int:
    import db
    from open_play import parse_pdf

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        path = tmp.name
    try:
        now = datetime.now()
        schedule = parse_pdf(path, now.year)
        if not schedule:
            _p(f"  {label}: no open play table found")
            return 0
        first_key = next(iter(schedule))
        month = int(first_key.split("/")[1])
        year = _infer_year(month, now)
        if year != now.year:
            schedule = parse_pdf(path, year)
        n = db.upsert_open_play(schedule)
        _p(f"  {label}: imported {n} day(s) for {month:02d}/{year}")
        return n
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _login_if_needed(page, user: str, password: str):
    """If the page shows a login form, fill and submit it (generic)."""
    has_pw = page.locator('input[type="password"]').count() > 0
    if not has_pw:
        _p("no login form visible — already logged in?")
        return
    # Log the login form structure
    info = page.evaluate("""() =>
        Array.from(document.querySelectorAll('input, button')).map(el =>
            `${el.tagName}[${el.type || ''}] name=${el.name || ''} id=${el.id || ''} ` +
            `placeholder=${el.placeholder || ''} value=${(el.value || '').slice(0, 20)}`)
            .slice(0, 25)
    """)
    for line in info:
        _p(f"  [login el] {line}")

    # Username: the text/email/number input nearest the password field
    user_box = page.locator(
        'input[type="text"], input[type="email"], input[type="number"], '
        'input[name*="user" i], input[name*="member" i]').first
    user_box.fill(user)
    page.locator('input[type="password"]').first.fill(password)
    btn = page.locator(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Log"), button:has-text("Sign")').first
    try:
        btn.click(timeout=10_000, force=True)
    except Exception:
        page.keyboard.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    page.wait_for_timeout(1500)
    _p(f"after login → {page.url}")


def fetch() -> int:
    user = os.environ.get("MEMBERS_USER")
    password = os.environ.get("MEMBERS_PASS")
    if not user or not password:
        _p("MEMBERS_USER/MEMBERS_PASS not set — skipping")
        return 0
    base = os.environ.get("MEMBERS_URL", "https://members.burhillgolf-club.co.uk/")

    imported = 0
    with sync_playwright() as pw:
        headless = os.environ.get("HEADLESS", "0") == "1"
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(base, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(1500)
            _p(f"landed → {page.url}")
            _login_if_needed(page, user, password)

            # Collect candidate links: anything mentioning open play, or a PDF
            links = page.evaluate("""() =>
                Array.from(document.querySelectorAll('a')).map(a => ({
                    text: (a.innerText || '').trim().slice(0, 80),
                    href: a.href || ''
                })).filter(l => l.href)
            """)
            _p(f"{len(links)} links on page")
            for l in links[:60]:
                _p(f"  [link] '{l['text']}' → {l['href'][:120]}")

            cands = [l for l in links
                     if ".pdf" in l["href"].lower()
                     or re.search(r"open\s*play", l["text"], re.I)
                     or re.search(r"open\s*play", l["href"], re.I)]
            _p(f"{len(cands)} open-play/PDF candidate link(s)")

            seen = set()
            for l in cands[:6]:
                href = l["href"]
                if href in seen:
                    continue
                seen.add(href)
                _p(f"fetching candidate: '{l['text']}' {href[:120]}")
                try:
                    resp = context.request.get(href, timeout=45_000)
                    ctype = resp.headers.get("content-type", "")
                    body = resp.body()
                    _p(f"  → {resp.status} {ctype} {len(body)} bytes")
                    if "pdf" in ctype.lower() or body[:5] == b"%PDF-":
                        imported += 1 if _import_pdf_bytes(body, l["text"] or href) else 0
                    elif "html" in ctype.lower():
                        # Maybe a page that hosts the actual PDF — scan it
                        sub = re.findall(r'href="([^"]+\.pdf[^"]*)"', body.decode("utf-8", "replace"), re.I)
                        for s_href in sub[:3]:
                            from urllib.parse import urljoin
                            full = urljoin(href, s_href)
                            _p(f"  nested pdf link: {full[:120]}")
                            r2 = context.request.get(full, timeout=45_000)
                            if r2.body()[:5] == b"%PDF-":
                                imported += 1 if _import_pdf_bytes(r2.body(), full) else 0
                except Exception as e:
                    _p(f"  candidate failed: {str(e)[:120]}")

            if not cands:
                try:
                    body = page.evaluate(
                        "() => (document.body && document.body.innerText || '').slice(0, 800)")
                    _p(f"[page text] {body}")
                except Exception:
                    pass
        finally:
            context.close()
            browser.close()

    _p(f"done — {imported} PDF(s) imported")
    return imported


if __name__ == "__main__":
    fetch()
