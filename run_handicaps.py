"""
Refresh WHS handicap indexes from the England Golf platform.

Logs into whsplatform.englandgolf.org with one set of credentials and looks
up each portal user's CDH number, storing the handicap index on their user
record. Runs weekly; can be run manually:

    python3 run_handicaps.py

Environment variables:
    EG_URL   — default https://whsplatform.englandgolf.org/
    EG_USER  — England Golf / WHS platform login (email)
    EG_PASS  — password
"""
import json
import os
import re

from playwright.sync_api import sync_playwright


def _p(msg):
    print(f"[handicaps] {msg}", flush=True)


HCP_RE = re.compile(r"\b(\+?\d{1,2}\.\d)\b")


def _login(page, user: str, password: str):
    if page.locator('input[type="password"]').count() == 0:
        # Maybe behind a Login link/button
        for sel in ('a:has-text("Log in")', 'a:has-text("Login")',
                    'button:has-text("Log in")', 'a:has-text("Sign in")'):
            loc = page.locator(sel).first
            if loc.count() > 0:
                _p(f"clicking login control {sel}")
                loc.click(force=True)
                page.wait_for_load_state("domcontentloaded", timeout=30_000)
                page.wait_for_timeout(1500)
                break
    if page.locator('input[type="password"]').count() == 0:
        _p("no login form found — dumping inputs")
        info = page.evaluate("""() =>
            Array.from(document.querySelectorAll('input, button, a')).map(el =>
                `${el.tagName}[${el.type || ''}] name=${el.name || ''} id=${el.id || ''} ` +
                `text=${((el.innerText || el.value || '')).trim().slice(0, 30)}`).slice(0, 40)
        """)
        for line in info:
            _p(f"  [el] {line}")
        return False

    info = page.evaluate("""() =>
        Array.from(document.querySelectorAll('input')).map(el =>
            `input[${el.type}] name=${el.name || ''} id=${el.id || ''} ` +
            `placeholder=${el.placeholder || ''}`).slice(0, 15)
    """)
    for line in info:
        _p(f"  [login el] {line}")

    user_box = page.locator(
        'input[type="email"], input[type="text"], '
        'input[name*="user" i], input[name*="email" i]').first
    user_box.fill(user)
    page.locator('input[type="password"]').first.fill(password)
    btn = page.locator(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Log"), button:has-text("Sign")').first
    try:
        btn.click(timeout=10_000, force=True)
    except Exception:
        page.keyboard.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=45_000)
    page.wait_for_timeout(3000)
    _p(f"after login → {page.url}")
    return True


def _lookup_cdh(page, base: str, cdh: str):
    """Search the platform for a CDH number; return (handicap, detail) or None."""
    # Try the obvious search/lookup pages first; dump links once so the log
    # shows us where the lookup actually lives.
    links = page.evaluate("""() =>
        Array.from(document.querySelectorAll('a')).map(a => ({
            text: (a.innerText || '').trim().slice(0, 60), href: a.href || ''
        })).filter(l => l.text || l.href)
    """)
    nav = [l for l in links if re.search(r"hand|look|search|golfer",
                                          (l["text"] + l["href"]), re.I)]
    for l in nav[:10]:
        _p(f"  [nav candidate] '{l['text']}' → {l['href'][:100]}")
    if nav:
        page.goto(nav[0]["href"], wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2000)
        _p(f"  lookup page → {page.url}")

    boxes = page.locator('input[type="text"], input[type="search"], input[type="number"]')
    if boxes.count() == 0:
        _p("  no search box found — dumping page")
        body = page.evaluate(
            "() => ((document.body && document.body.innerText) || '').slice(0, 600)")
        _p(f"  [page text] {body}")
        return None

    box = boxes.first
    box.fill(cdh)
    page.keyboard.press("Enter")
    page.wait_for_timeout(3500)
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass

    body = page.evaluate(
        "() => ((document.body && document.body.innerText) || '').slice(0, 1200)")
    _p(f"  [results text] {body[:600]}")
    m = HCP_RE.search(body)
    if m:
        return m.group(1), body[:200]
    return None


def refresh() -> int:
    import db
    user = os.environ.get("EG_USER")
    password = os.environ.get("EG_PASS")
    if not user or not password:
        _p("EG_USER/EG_PASS not set — skipping")
        return 0
    base = os.environ.get("EG_URL", "https://whsplatform.englandgolf.org/")

    players = db.users_with_cdh()
    if not players:
        _p("no users have a CDH number set — nothing to do")
        return 0
    _p(f"{len(players)} player(s) with CDH numbers")

    updated = 0
    with sync_playwright() as pw:
        headless = os.environ.get("HEADLESS", "0") == "1"
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(base, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2500)
            _p(f"landed → {page.url}")
            if not _login(page, user, password):
                return 0

            for p_row in players:
                _p(f"looking up {p_row['name']} (CDH {p_row['cdh_number']})")
                try:
                    res = _lookup_cdh(page, base, p_row["cdh_number"])
                    if res:
                        hcp, detail = res
                        db.update_user_handicap(p_row["id"], hcp)
                        _p(f"  ✅ {p_row['name']}: {hcp}")
                        updated += 1
                    else:
                        _p(f"  ⚠️ no handicap found for {p_row['name']}")
                except Exception as e:
                    _p(f"  lookup failed: {str(e)[:120]}")
                # Back to the start for the next search
                try:
                    page.goto(base, wait_until="domcontentloaded", timeout=30_000)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
        finally:
            context.close()
            browser.close()

    _p(f"done — {updated} handicap(s) updated")
    return updated


if __name__ == "__main__":
    refresh()
