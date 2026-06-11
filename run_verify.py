"""
Verify a playing-partner member name against Burhill's member search.

Walks into the booking flow far enough to reach the participants page,
types the name into the member field with the search box ticked, submits,
and captures whatever matches Burhill returns. No booking is made — we
never select a date or slot.

Usage:
    python3 run_verify.py --user-id 1 --query "Johnson"
"""
import argparse
import json
import os
import sys

import db
import crypto
from book import (_login, _dump_forms, _submit_participants_form,
                  _participants_state, BASE_URL)
from playwright.sync_api import sync_playwright


def _p(msg):
    print(msg, flush=True)


def _capture_matches(page, query: str) -> dict:
    """Collect anything that looks like member search results."""
    return page.evaluate("""(query) => {
        const q = query.toLowerCase();
        const out = {selects: [], clickables: [], inputs: []};
        for (const sel of Array.from(document.querySelectorAll('select'))) {
            const opts = Array.from(sel.options)
                .map(o => ({text: (o.text || '').trim(), value: o.value}))
                .filter(o => o.text);
            if (opts.length) out.selects.push({name: sel.name || '', options: opts});
        }
        for (const el of Array.from(document.querySelectorAll('a, button, td[onclick], span[onclick]'))) {
            const txt = (el.innerText || '').trim();
            if (txt && txt.toLowerCase().includes(q)) {
                out.clickables.push(txt.slice(0, 80));
            }
        }
        for (const inp of Array.from(document.querySelectorAll('input[type=text], input[type=hidden]'))) {
            const v = (inp.value || '').trim();
            if (v && v.toLowerCase().includes(q)) {
                out.inputs.push({name: inp.name || '', value: v.slice(0, 80)});
            }
        }
        out.body = document.body.innerText.trim().slice(0, 800);
        return out;
    }""", query)


def verify(page, query: str) -> dict:
    # Reach the participants page: book_start → Golf Club Tee Times → a course
    page.goto(f"{BASE_URL}/book_start.php", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(500)
    page.get_by_role("link", name="Golf Club Tee Times").first.click(timeout=30_000, force=True)
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    page.wait_for_timeout(400)
    # Any course works — we never get past participants
    page.get_by_role("link", name="New Course").first.click(timeout=30_000, force=True)
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    page.wait_for_timeout(400)
    _p(f"  [participants] → {page.url}")

    # Two players so the member field exists
    try:
        page.select_option('select[name="NumPeople"]', "2")
        page.wait_for_timeout(800)
    except Exception as e:
        _p(f"  [NumPeople select failed] {str(e)[:100]}")
    if _participants_state(page).get("curNum") != "2":
        _submit_participants_form(page, "1", "NumPeople submit")

    # Type the name into the member field, tick member search, submit confirm
    res = page.evaluate("""(q) => {
        const out = [];
        const t = document.querySelector('input[name="BookMemb1"]');
        if (t) { t.value = q; out.push('BookMemb1=' + q); }
        const ms = document.querySelector('input[name="mschk1"]');
        if (ms) { ms.checked = true; out.push('mschk1 ticked'); }
        const g = document.querySelector('input[name="BookNonMemb1"]');
        if (g) { g.checked = false; out.push('guest cleared'); }
        return out.join(', ') || 'no member inputs found';
    }""", query)
    _p(f"  [fill] {res}")
    _submit_participants_form(page, "2", "Member search submit")

    _p(f"  [after search] → {page.url}")
    _dump_forms(page, "after member search")
    captured = _capture_matches(page, query)
    _p(f"  [captured] {json.dumps(captured)[:1500]}")

    # Distil a friendly match list: select options or clickable names
    matches = []
    for sel in captured["selects"]:
        for o in sel["options"]:
            if query.lower() in o["text"].lower():
                matches.append(o["text"])
    matches.extend(c for c in captured["clickables"] if c not in matches)
    for i in captured["inputs"]:
        if i["name"].startswith("BookMembName") and i["value"] not in matches:
            matches.append(i["value"])

    return {
        "url": page.url,
        "matches": matches[:10],
        "raw": captured,
    }


def run(user_id: int, query: str):
    _p(f"[run_verify] start user_id={user_id} query={query!r}")
    user = db.get_user_by_id(user_id)
    if not user or not user.get("burhill_user") or not user.get("burhill_pass"):
        db.set_member_search(user_id, query, "failed",
                             json.dumps({"error": "No Burhill credentials"}))
        sys.exit(1)

    os.environ["BURHILL_USERNAME"] = user["burhill_user"]
    os.environ["BURHILL_PASSWORD"] = crypto.decrypt(user["burhill_pass"])
    from config import CREDENTIALS
    CREDENTIALS["username"] = user["burhill_user"]
    CREDENTIALS["password"] = crypto.decrypt(user["burhill_pass"])

    try:
        with sync_playwright() as pw:
            headless = os.environ.get("HEADLESS", "0") == "1"
            browser = pw.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            try:
                _login(page)
                _p("[run_verify] logged in")
                result = verify(page, query)
                db.set_member_search(user_id, query, "done", json.dumps(result))
                _p(f"[run_verify] ✅ {len(result['matches'])} match(es): {result['matches']}")
            finally:
                context.close()
                browser.close()
    except Exception as e:
        import traceback
        _p(f"[run_verify] EXCEPTION: {e}")
        _p(traceback.format_exc())
        db.set_member_search(user_id, query, "failed",
                             json.dumps({"error": f"{type(e).__name__}: {str(e)[:200]}"}))
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--query", type=str, required=True)
    args = parser.parse_args()
    run(args.user_id, args.query)
