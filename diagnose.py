"""
Logs in and follows the booking flow via real clicks, printing form/link structure.
Run: python3 diagnose.py
"""
from playwright.sync_api import sync_playwright
from config import CREDENTIALS, BOOKING

BASE_URL = "https://www.e-s-p.com/elitelive"
CLUB_ID  = "675"

def dump_page(page, label):
    print(f"\n{'='*60}\n  {label}\n  URL: {page.url}\n{'='*60}")
    data = page.evaluate("""() => ({
        links: [...document.querySelectorAll('a[href]')].map(a => ({text: a.innerText.trim().slice(0,50), href: a.href})).filter(l => l.text),
        forms: [...document.querySelectorAll('form')].map(f => ({
            action: f.action, method: f.method,
            fields: [...f.querySelectorAll('input,select,button')].map(i => ({
                tag: i.tagName, name: i.name, type: i.type||'', value: i.value.slice(0,100),
                options: i.tagName==='SELECT' ? [...i.options].map(o=>({val:o.value.slice(0,80),text:o.text.slice(0,40)})) : undefined
            }))
        }))
    })""")
    print("Links:")
    for l in data['links']: print(f"  [{l['text']}] -> {l['href']}")
    print("Forms:")
    for f in data['forms']:
        print(f"  {f['method'].upper()} -> {f['action']}")
        for fld in f['fields']:
            if fld['tag'] == 'SELECT':
                print(f"    SELECT name={fld['name']} options={[o['text'] for o in (fld['options'] or [])]}")
            else:
                print(f"    {fld['tag']} name={fld['name']} type={fld['type']} value={fld['value'][:80]}")

def nav_click(page, selector):
    """Click and wait for navigation to complete."""
    with page.expect_navigation(wait_until="networkidle"):
        page.evaluate(f"document.querySelector('{selector}').click()")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=200)
    page = browser.new_page()

    # Login
    page.goto(f"{BASE_URL}/?clubid={CLUB_ID}")
    page.wait_for_url(lambda url: "login.php" in url, timeout=10_000)
    page.fill('input[name="username"]', CREDENTIALS["username"])
    page.fill('input[name="password"]', CREDENTIALS["password"])
    with page.expect_navigation(wait_until="networkidle"):
        page.evaluate("document.querySelector('input[type=submit]').click()")
    print(f"✓ Logged in → {page.url}")

    # Step 2: Make Booking
    with page.expect_navigation(wait_until="networkidle"):
        page.evaluate("document.querySelector('input[value=\"Make Booking\"]').closest('form').submit()")
    dump_page(page, "AFTER Make Booking (book_group category)")

    # Step 3: Golf Club Tee Times link
    with page.expect_navigation(wait_until="networkidle"):
        page.evaluate("document.querySelector('a[href*=\"Golf+Club+Tee+Times\"]').click()")
    dump_page(page, "AFTER Golf Club Tee Times")

    # Step 4: Old Course link
    with page.expect_navigation(wait_until="networkidle"):
        page.evaluate("document.querySelector('a[href*=\"Old+Course\"]').click()")
    dump_page(page, "AFTER Old Course (date picker)")

    # Step 5: on book_participants.php
    # - Select player count and submit gotdata=1 form
    # - Then submit gotdata=2 form to proceed to date picker
    if "book_participants" in page.url:
        players = str(BOOKING["players"])
        page.select_option('select[name="NumPeople"]', players)
        # Submit gotdata=1 form
        with page.expect_navigation(wait_until="load"):
            page.locator('input[name="gotdata"][value="1"]').locator('..').locator('..').evaluate("f => f.submit()")
        page.wait_for_load_state("domcontentloaded")
        dump_page(page, "AFTER NumPeople submitted (gotdata=1)")

        # Submit gotdata=2 form (member confirmation)
        with page.expect_navigation(wait_until="load"):
            page.locator('input[name="gotdata"][value="2"]').evaluate("el => el.closest('form').submit()")
        page.wait_for_load_state("domcontentloaded")
        dump_page(page, "AFTER participants confirmed (gotdata=2)")

    # Step 6: navigate to target date using the URL directly (session already set)
    parts = BOOKING["date"].split("/")  # DD/MM/YYYY
    date_url_param = f"{parts[0]}%2F{parts[1]}%2F{parts[2][2:]}"  # 18%2F06%2F26
    date_url = f"{BASE_URL}/book_date.php?gotdata=1&StartDate={date_url_param}&EndDate={date_url_param}&"
    # Click date 18 link from within the calendar page
    target_day = BOOKING["date"].split("/")[0].lstrip("0")  # "18"
    date_link = page.query_selector(f'a:text-is("{target_day}")')
    if not date_link:
        date_link = page.query_selector('a[href*="book_date"][href*="StartDate"]')
    if date_link:
        href = date_link.get_attribute("href")
        print(f"\nClicking date [{date_link.inner_text()}] -> {href}")
        date_link.click()
        page.wait_for_load_state("domcontentloaded", timeout=45_000)
        page.wait_for_timeout(3000)
        dump_page(page, "AFTER DATE CLICK")
        html = page.content()
        print("\n--- RAW HTML (first 5000 chars) ---")
        print(html[:5000])
    else:
        print(f"No date link found for day {target_day}")

    browser.close()
