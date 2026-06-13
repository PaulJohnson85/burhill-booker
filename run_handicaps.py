"""
Refresh WHS handicap indexes from the England Golf "My Golf" site.

Logs into englandgolf.org with one membership login, then uses the member
search to look up each portal user's CDH number and store their handicap
index. Runs weekly; can be run manually:

    python3 run_handicaps.py

Environment variables:
    EG_LOGIN_URL — default https://www.englandgolf.org/my-golf-login
    EG_USER      — England Golf membership number (login)
    EG_PASS      — password
"""
import os
import re

from playwright.sync_api import sync_playwright


def _p(msg):
    print(f"[handicaps] {msg}", flush=True)


# Handicap index like 12.4, 7.0, +1.3, 54.0
HCP_RE = re.compile(r"(\+?\d{1,2}\.\d)")


def _dump(page, label):
    try:
        body = page.evaluate(
            "() => ((document.body && document.body.innerText) || '').slice(0, 900)")
        _p(f"  [{label} text] {body}")
    except Exception:
        pass


def _login(page, user, password):
    # The login form may be on this page or behind a "Log in" link.
    if page.locator('input[type="password"]').count() == 0:
        for sel in ('a:has-text("Log in")', 'a:has-text("Login")',
                    'a:has-text("Sign in")', 'button:has-text("Log in")'):
            loc = page.locator(sel).first
            if loc.count() > 0:
                _p(f"clicking {sel}")
                try:
                    loc.click(timeout=8000, force=True)
                    page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass
                break

    # Accept a cookie banner if present (can cover the form)
    for sel in ('button:has-text("Accept")', 'button:has-text("Allow all")',
                'button:has-text("I agree")', '#onetrust-accept-btn-handler'):
        loc = page.locator(sel).first
        if loc.count() > 0:
            try:
                loc.click(timeout=4000, force=True)
                page.wait_for_timeout(800)
            except Exception:
                pass
            break

    if page.locator('input[type="password"]').count() == 0:
        _p("no password field found — dumping inputs")
        info = page.evaluate("""() =>
            Array.from(document.querySelectorAll('input, button, a')).map(el =>
                `${el.tagName}[${el.type||''}] name=${el.name||''} id=${el.id||''} `+
                `ph=${el.placeholder||''} text=${((el.innerText||el.value||'')).trim().slice(0,30)}`)
                .slice(0, 40)
        """)
        for line in info:
            _p(f"  [el] {line}")
        return False

    info = page.evaluate("""() =>
        Array.from(document.querySelectorAll('input')).map(el =>
            `input[${el.type}] name=${el.name||''} id=${el.id||''} ph=${el.placeholder||''}`)
            .slice(0, 15)
    """)
    for line in info:
        _p(f"  [login el] {line}")

    user_box = page.locator(
        'input[type="text"], input[type="email"], input[type="number"], '
        'input[name*="user" i], input[name*="member" i], input[name*="login" i]').first
    user_box.fill(user)
    page.locator('input[type="password"]').first.fill(password)
    btn = page.locator(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Log in"), button:has-text("Sign in")').first
    try:
        btn.click(timeout=10_000, force=True)
    except Exception:
        page.keyboard.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=45_000)
    page.wait_for_timeout(3500)
    _p(f"after login → {page.url}")
    return True


def _find_search(page):
    """Navigate to the member-search part of the site, returning the page/frame
    that holds a search box (or None)."""
    # Try links that look like member search
    links = page.evaluate("""() =>
        Array.from(document.querySelectorAll('a, button')).map(a => ({
            text: (a.innerText||'').trim().slice(0,50),
            href: a.href || ''
        })).filter(l => l.text || l.href)
    """)
    for l in links:
        if re.search(r"member.?search|find.?(a.?)?(member|golfer)|search", l["text"], re.I):
            _p(f"  [search link] '{l['text']}' → {l['href'][:100]}")
    target = next((l for l in links
                   if re.search(r"member.?search|find.?(a.?)?(member|golfer)",
                                l["text"], re.I) and l["href"]), None)
    if target:
        try:
            page.goto(target["href"], wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2000)
            _p(f"  search page → {page.url}")
        except Exception:
            pass
    return page


def _lookup(page, cdh):
    boxes = page.locator(
        'input[type="text"], input[type="search"], input[type="number"], '
        'input[name*="cdh" i], input[name*="search" i]')
    if boxes.count() == 0:
        _p("  no search box found")
        _dump(page, "search page")
        return None
    box = boxes.first
    box.click(force=True)
    box.fill(cdh)
    # A search button if present, else Enter
    btn = page.locator('button:has-text("Search"), input[type="submit"], '
                       'button[type="submit"]').first
    try:
        if btn.count() > 0:
            btn.click(timeout=6000, force=True)
        else:
            page.keyboard.press("Enter")
    except Exception:
        page.keyboard.press("Enter")
    page.wait_for_timeout(4000)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    body = page.evaluate(
        "() => ((document.body && document.body.innerText) || '')")
    _p(f"  [result] {body[:500].strip()}")
    # Prefer a value near a 'Handicap' label
    m = re.search(r"hand[ic ]*\w*[^0-9+]{0,20}(\+?\d{1,2}\.\d)", body, re.I)
    if not m:
        m = HCP_RE.search(body)
    return m.group(1) if m else None


def refresh() -> int:
    import db
    user = os.environ.get("EG_USER")
    password = os.environ.get("EG_PASS")
    if not user or not password:
        _p("EG_USER/EG_PASS not set — skipping")
        return 0
    login_url = os.environ.get("EG_LOGIN_URL",
                               "https://www.englandgolf.org/my-golf-login")

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
            page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2500)
            _p(f"landed → {page.url}")
            if not _login(page, user, password):
                return 0
            _find_search(page)
            search_url = page.url

            for p_row in players:
                _p(f"looking up {p_row['name']} (CDH {p_row['cdh_number']})")
                try:
                    hcp = _lookup(page, p_row["cdh_number"])
                    if hcp:
                        db.update_user_handicap(p_row["id"], hcp)
                        _p(f"  ✅ {p_row['name']}: {hcp}")
                        updated += 1
                    else:
                        _p(f"  ⚠️ no handicap found for {p_row['name']}")
                except Exception as e:
                    _p(f"  lookup failed: {str(e)[:120]}")
                # Back to the search page for the next lookup
                try:
                    page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
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
