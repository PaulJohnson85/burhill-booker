"""Inspect book_participants.php with NumPeople=2."""
from playwright.sync_api import sync_playwright
from config import CREDENTIALS

BASE_URL = "https://www.e-s-p.com/elitelive"
CLUB_ID  = "675"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=300)
    page = browser.new_page()

    page.goto(f"{BASE_URL}/?clubid={CLUB_ID}")
    page.wait_for_url(lambda url: "login.php" in url)
    page.fill('input[name="username"]', CREDENTIALS["username"])
    page.fill('input[name="password"]', CREDENTIALS["password"])
    with page.expect_navigation(wait_until="load"):
        page.evaluate("document.querySelector('input[type=submit]').click()")

    # Make Booking
    with page.expect_navigation(wait_until="load"):
        page.evaluate("document.querySelector('input[value=\"Make Booking\"]').closest('form').submit()")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(400)

    # Golf Club Tee Times
    with page.expect_navigation(wait_until="load"):
        page.evaluate("document.querySelector('a[href*=\"Golf+Club+Tee+Times\"]').click()")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(400)

    # Tee Times (Any course)
    with page.expect_navigation(wait_until="load"):
        page.evaluate("document.querySelector('a[href*=\"Tee+Times\"]').click()")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(400)

    print(f"On: {page.url}")

    # Select 2 players
    page.select_option('select[name="NumPeople"]', "2")
    with page.expect_navigation(wait_until="load"):
        page.locator('input[name="gotdata"][value="1"]').evaluate("el => el.closest('form').submit()")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(600)

    print(f"\nAfter NumPeople=2 submit: {page.url}")

    # Dump all forms
    data = page.evaluate("""() => ({
        forms: [...document.querySelectorAll('form')].map(f => ({
            action: f.action, method: f.method,
            fields: [...f.querySelectorAll('input,select,textarea')].map(i => ({
                tag: i.tagName, name: i.name, type: i.type||'',
                value: i.value.slice(0,80), placeholder: i.placeholder||''
            }))
        }))
    })""")

    for f in data['forms']:
        relevant = [x for x in f['fields'] if x['name'] and x['name'] not in ('', )]
        if not relevant:
            continue
        print(f"\n  FORM → {f['action']}")
        for fld in relevant:
            print(f"    {fld['tag']} name={fld['name']} type={fld['type']} value='{fld['value']}' placeholder='{fld['placeholder']}'")

    input("\nPress Enter to close…")
    browser.close()
