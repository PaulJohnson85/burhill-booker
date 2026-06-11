"""Inspect book_questionnaire.php and the tee times page for the 'Golf/Any' flow."""
from playwright.sync_api import sync_playwright
from config import CREDENTIALS, BOOKING

BASE_URL = "https://www.e-s-p.com/elitelive"
CLUB_ID  = "675"

def dump(page, label):
    print(f"\n=== {label} | {page.url} ===")
    data = page.evaluate("""() => ({
        links: [...document.querySelectorAll('a[href]')].map(a=>({t:a.innerText.trim().slice(0,40),h:a.href.slice(0,80)})).filter(l=>l.t),
        forms: [...document.querySelectorAll('form')].map(f=>({
            action:f.action.slice(0,60), method:f.method,
            fields:[...f.querySelectorAll('input,select,textarea')].map(i=>({
                tag:i.tagName,name:i.name,type:i.type||'',value:i.value.slice(0,80),
                options:i.tagName==='SELECT'?[...i.options].map(o=>o.text.slice(0,30)):undefined
            }))
        }))
    })""")
    print("Links:", [(l['t'],l['h']) for l in data['links'] if 'book' in l['h'].lower()])
    for f in data['forms']:
        fields = [x for x in f['fields'] if x['name']]
        if not fields: continue
        print(f"  FORM {f['method'].upper()} → {f['action']}")
        for fld in fields:
            if fld['tag']=='SELECT':
                print(f"    SELECT {fld['name']} opts={fld['options']}")
            else:
                print(f"    {fld['tag']} {fld['name']} type={fld['type']} val='{fld['value']}'")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=200)
    page = browser.new_page()

    page.goto(f"{BASE_URL}/?clubid={CLUB_ID}")
    page.wait_for_url(lambda url: "login.php" in url)
    page.fill('input[name="username"]', CREDENTIALS["username"])
    page.fill('input[name="password"]', CREDENTIALS["password"])
    with page.expect_navigation(wait_until="load"):
        page.evaluate("document.querySelector('input[type=submit]').click()")
    page.wait_for_load_state("domcontentloaded")

    def nav(fn):
        with page.expect_navigation(wait_until="load", timeout=30_000):
            fn()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(400)

    nav(lambda: page.evaluate("document.querySelector('input[value=\"Make Booking\"]').closest('form').submit()"))
    nav(lambda: page.evaluate("document.querySelector('a[href*=\"Golf+Club+Tee+Times\"]').click()"))
    nav(lambda: page.evaluate("document.querySelector('a[href*=\"Tee+Times\"]').click()"))
    page.select_option('select[name="NumPeople"]', str(BOOKING["players"]))
    nav(lambda: page.locator('input[name="gotdata"][value="1"]').evaluate("el => el.closest('form').submit()"))

    if "book_participants" in page.url:
        for i in range(1, BOOKING["players"]):
            page.evaluate(f"""const cb=document.querySelector('input[name="BookNonMemb{i}"]');if(cb)cb.checked=true;""")
        nav(lambda: page.locator('input[name="gotdata"][value="2"]').evaluate("el => el.closest('form').submit()"))

    # Click date 17 — use direct click (doesn't fire clean load event)
    target_day = BOOKING["date"].split("/")[0].lstrip("0")
    page.locator(f'a[href*="StartDate={target_day}"]').first.click()
    page.wait_for_load_state("domcontentloaded", timeout=45_000)
    page.wait_for_timeout(1000)

    dump(page, "AFTER DATE CLICK")

    # If questionnaire, dump and try submitting first form
    if "questionnaire" in page.url:
        print("\n→ Questionnaire page detected. Trying to submit...")
        # Try submitting the first submit button
        btns = page.query_selector_all('input[type=submit], button[type=submit]')
        print(f"  Submit buttons: {[b.get_attribute('value') or b.inner_text() for b in btns]}")

    browser.close()
