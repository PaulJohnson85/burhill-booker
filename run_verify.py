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
    """Collect anything that looks like member search results. Never raises —
    retries once after a settle delay, then falls back to raw HTML scraping."""
    for attempt in range(2):
        try:
            return _capture_matches_js(page, query)
        except Exception as e:
            _p(f"  [capture attempt {attempt} failed] {str(e)[:120]}")
            page.wait_for_timeout(1500)
    # Fallback: regex over the raw HTML
    out = {"selects": [], "clickables": [], "inputs": [], "error": "", "body": ""}
    try:
        import re as _re
        html = page.content()
        text = _re.sub(r"<[^>]+>", " ", html)
        text = _re.sub(r"\s+", " ", text).strip()
        out["body"] = text[:800]
        m = _re.search(r"Error with participant[^<.]*", text, _re.I)
        if m:
            out["error"] = m.group(0).strip()
        for mm in _re.finditer(r"<option[^>]*>([^<]+)</option>", html, _re.I):
            t = mm.group(1).strip()
            if t and query.lower() in t.lower():
                out["selects"].append({"name": "?", "options": [{"text": t, "value": ""}]})
    except Exception as e:
        out["error"] = f"capture fallback failed: {str(e)[:100]}"
    return out


def _capture_matches_js(page, query: str) -> dict:
    return page.evaluate("""(query) => {
        const q = query.toLowerCase();
        const out = {selects: [], clickables: [], inputs: [], error: '', body: ''};
        try {
            for (const sel of Array.from(document.querySelectorAll('select'))) {
                const opts = Array.from(sel.options || [])
                    .map(o => ({text: ((o && o.text) || '').trim(), value: (o && o.value) || ''}))
                    .filter(o => o.text);
                if (opts.length) out.selects.push({name: sel.name || '', options: opts});
            }
            for (const el of Array.from(document.querySelectorAll('a, button, td[onclick], span[onclick]'))) {
                const txt = ((el && (el.innerText || el.textContent)) || '').trim();
                if (txt && txt.toLowerCase().includes(q)) {
                    out.clickables.push(txt.slice(0, 80));
                }
            }
            for (const inp of Array.from(document.querySelectorAll('input[type=text], input[type=hidden]'))) {
                const v = ((inp && inp.value) || '').trim();
                if (v && v.toLowerCase().includes(q)) {
                    out.inputs.push({name: inp.name || '', value: v.slice(0, 80)});
                }
            }
            const body = (document.body && (document.body.innerText || document.body.textContent)) || '';
            out.body = body.trim().slice(0, 800);
            const m = body.match(/Error with participant[^\\n]*/i);
            if (m) out.error = m[0].trim();
        } catch (e) {
            out.error = out.error || ('capture error: ' + e.message);
        }
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

    # Type the name into the member field and clear the guest box
    res = page.evaluate("""(q) => {
        const out = [];
        const t = document.querySelector('input[name="BookMemb1"]');
        if (t) { t.value = q; out.push('BookMemb1=' + q); }
        const g = document.querySelector('input[name="BookNonMemb1"]');
        if (g) { g.checked = false; out.push('guest cleared'); }
        return out.join(', ') || 'no member inputs found';
    }""", query)
    _p(f"  [fill] {res}")

    # mschk1 is not a form flag — on the real site it's the control that opens
    # the member search UI via JS. Inspect it, then really click it.
    info = page.evaluate("""() => {
        const ms = document.querySelector('input[name="mschk1"]');
        const out = {outer: ms ? ms.outerHTML.slice(0, 300) : null, handlers: []};
        if (ms) {
            for (const a of ['onclick', 'onchange', 'onmouseup']) {
                const v = ms.getAttribute(a);
                if (v) out.handlers.push(a + '=' + v.slice(0, 150));
            }
        }
        return out;
    }""")
    _p(f"  [mschk1] {json.dumps(info)}")

    # Read the page JS that the checkbox drives — set_msp / set_membernotselected
    js_src = page.evaluate("""() => {
        const out = {};
        for (const fn of ['set_msp', 'set_membernotselected', 'set_memberselected']) {
            try { out[fn] = window[fn] ? window[fn].toString().slice(0, 600) : 'undefined'; }
            catch (e) { out[fn] = 'error'; }
        }
        out.snippets = Array.from(document.querySelectorAll('script:not([src])'))
            .map(s => s.textContent || '')
            .filter(t => /msp|membsearch|membernot/i.test(t))
            .map(t => t.replace(/\\s+/g, ' ').slice(0, 1200));
        return out;
    }""")
    _p(f"  [page js] {json.dumps(js_src)[:2500]}")

    popup = None
    try:
        with page.expect_popup(timeout=5_000) as pop_info:
            # Real click so the onchange handler (set_msp) actually fires
            page.locator('input[name="mschk1"]').click(force=True)
        popup = pop_info.value
        _p(f"  [popup opened] {popup.url}")
    except Exception:
        _p("  [no popup window after mschk1 click]")

    # What did the handler change? Diff the member-related inputs
    state = page.evaluate("""() => {
        const out = {};
        for (const el of Array.from(document.querySelectorAll('input'))) {
            if (/msp|mschk|BookMemb|BookNonMemb/i.test(el.name || el.id || '')) {
                out[(el.name || el.id) + ':' + el.type] =
                    el.type === 'checkbox' ? (el.checked + '/' + el.value) : el.value;
            }
        }
        return out;
    }""")
    _p(f"  [post-click state] {json.dumps(state)[:800]}")

    # Now submit the participants form — with set_msp fired, the server should
    # perform the member search rather than an exact lookup
    _submit_participants_form(page, "2", "Member search submit")

    target = popup if popup is not None else page
    try:
        target.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    target.wait_for_timeout(1500)
    _p(f"  [search UI] url={target.url}")
    _p(f"  [frames] {[f.url for f in target.frames]}")
    _dump_forms(target, "member search UI")

    # If the search UI (popup or an iframe) has its own search box, run the
    # search: fill the first text input and submit its form / press Enter.
    contexts = [target.main_frame] + list(target.frames[1:]) if popup else list(page.frames)
    for frame in contexts:
        try:
            ran = frame.evaluate("""(q) => {
                const boxes = Array.from(document.querySelectorAll('input[type=text]'))
                    .filter(i => !/^BookMemb/.test(i.name || ''));
                if (!boxes.length) return 'no-box';
                const box = boxes[0];
                box.value = q;
                const form = box.form;
                if (form) {
                    const btn = Array.from(form.elements).find(el =>
                        el.type === 'submit' || el.type === 'image' || el.type === 'button');
                    if (btn) { btn.click(); return 'searched via button ' + (btn.value || btn.name || '?'); }
                    form.submit();
                    return 'searched via form.submit';
                }
                box.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
                return 'searched via Enter';
            }""", query)
            if ran != "no-box":
                _p(f"  [search ran in {frame.url}] {ran}")
                break
        except Exception as e:
            _p(f"  [frame search failed {frame.url}] {str(e)[:80]}")

    target.wait_for_timeout(2500)
    try:
        target.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass
    _p(f"  [after search] url={target.url} frames={[f.url for f in target.frames]}")
    _dump_forms(target, "after member search")

    # Capture from the popup/page and all its frames
    captured = _capture_matches(target, query)
    for frame in target.frames[1:]:
        try:
            sub = frame.evaluate("""(q) => {
                const out = {options: [], links: [], body: ''};
                for (const sel of Array.from(document.querySelectorAll('select')))
                    for (const o of Array.from(sel.options || []))
                        if (o.text) out.options.push(o.text.trim());
                for (const a of Array.from(document.querySelectorAll('a, td[onclick], tr[onclick], button')))
                    if (((a.innerText || '').trim())) out.links.push(a.innerText.trim().slice(0, 80));
                out.body = ((document.body && document.body.innerText) || '').trim().slice(0, 600);
                return out;
            }""", query)
            _p(f"  [frame capture {frame.url}] {json.dumps(sub)[:1000]}")
            captured["clickables"].extend(
                l for l in sub["links"] if query.lower() in l.lower())
            captured["selects"].extend(
                [{"name": "frame", "options": [{"text": t, "value": ""}]}
                 for t in sub["options"] if query.lower() in t.lower()])
            if not captured.get("body"):
                captured["body"] = sub["body"]
        except Exception:
            continue
    _p(f"  [captured] {json.dumps(captured)[:1500]}")
    if popup is not None:
        try:
            popup.close()
        except Exception:
            pass

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

    # If the site accepted the name and moved on (e.g. to the calendar),
    # extract resolved members ("CODE - Name" lines) from the page
    if not matches and "book_participants" not in page.url:
        import re as _re
        for m in _re.finditer(r"\b[A-Z]{3,8}\d{1,3}\s*-\s*[A-Za-z'\- ]{3,40}",
                              captured.get("body", "")):
            t = m.group(0).strip()
            if t not in matches:
                matches.append(t)
        if not matches:
            matches.append(f"'{query}' — accepted by Burhill")
        _p(f"  [accepted] search moved to {page.url}")

    return {
        "url": page.url,
        "matches": matches[:10],
        "error": captured.get("error", ""),
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
