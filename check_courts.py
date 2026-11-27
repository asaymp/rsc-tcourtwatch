import os
import re
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
CLUB_EMAIL = os.environ.get("CLUB_EMAIL")
CLUB_PASSWORD = os.environ.get("CLUB_PASSWORD")

STATE_FILE = "seen_courts.json"
BASE_URL = "https://clubautomation.com"

# Doubles courts only (Court 5 / ID "232" is excluded)
COURTS = {
    "272": "Court 1",
    "273": "Court 2",
    "274": "Court 3",
    "290": "Court 4",
    "233": "Court 6",
    "234": "Court 7",
    "235": "Court 8",
}

# Target hours by weekday (0=Mon ... 4=Fri, 5=Sat, 6=Sun)
SCHEDULE_WINDOWS = {
    4: ("17:00", "21:30"),  # Friday 5pm - 10pm
    5: ("08:00", "21:30"),  # Saturday 8am - 10pm
    6: ("08:00", "21:30"),  # Sunday 8am - 10pm
}


def load_seen_courts():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            content = f.read().strip()
            if content:
                return json.loads(content)
    return []


def save_seen_courts(courts):
    with open(STATE_FILE, "w") as f:
        json.dump(courts, f, indent=2)


def time_to_minutes(t_str):
    h, m = map(int, t_str.split(":"))
    return h * 60 + m


def minutes_to_12h(mins):
    h = (mins // 60) % 24
    m = mins % 60
    ampm = "am" if h < 12 else "pm"
    h12 = h if 1 <= h <= 12 else (12 if h == 0 else h - 12)
    return f"{h12}:{m:02d}{ampm}"


def parse_grid_html(html, target_dt, now_ottawa):
    day_label = target_dt.strftime("%a %b %d")
    weekday = target_dt.weekday()
    win_start_str, win_end_str = SCHEDULE_WINDOWS[weekday]
    win_start = time_to_minutes(win_start_str)
    win_end = time_to_minutes(win_end_str)

    soup = BeautifulSoup(html, "html.parser")
    all_tds = soup.find_all("td", id=re.compile(r"^court_\d+_row_\d{2}:\d{2}$"))
    drag_tds = [td for td in all_tds if "start_drag" in td.get("onmousedown", "")]
    print(f"[{day_label}] total cells={len(all_tds)}, bookable cells={len(drag_tds)}")

    open_by_time = {}
    now_minutes = now_ottawa.hour * 60 + now_ottawa.minute
    is_today = target_dt.date() == now_ottawa.date()

    for td in drag_tds:
        match = re.match(r"^court_(\d+)_row_(\d{2}:\d{2})$", td["id"])
        if not match:
            continue

        court_id, time_str = match.groups()
        if court_id not in COURTS:
            continue

        slot_mins = time_to_minutes(time_str)
        if slot_mins < win_start or slot_mins > win_end:
            continue
        if is_today and slot_mins <= now_minutes:
            continue

        open_by_time.setdefault(slot_mins, []).append(court_id)

    found_options = []
    used_in_same_court = set()

    # 1. Full 1 hour on the SAME court
    for t in sorted(open_by_time.keys()):
        t_next = t + 30
        if t_next in open_by_time:
            for cid in open_by_time[t]:
                if cid in open_by_time[t_next]:
                    time_range = f"{minutes_to_12h(t)}-{minutes_to_12h(t + 60)}"
                    found_options.append(f"{day_label}: {COURTS[cid]} ({time_range})")
                    used_in_same_court.add((t, cid))
                    used_in_same_court.add((t_next, cid))

    # 2. Adjacent 30-min slots on DIFFERENT courts
    for t in sorted(open_by_time.keys()):
        t_next = t + 30
        if t_next in open_by_time:
            first_courts = [c for c in open_by_time[t] if (t, c) not in used_in_same_court]
            second_courts = [c for c in open_by_time[t_next] if (t_next, c) not in used_in_same_court]

            for c1 in first_courts:
                for c2 in second_courts:
                    if c1 != c2:
                        t_mid = minutes_to_12h(t_next)
                        time_range = f"{minutes_to_12h(t)}-{minutes_to_12h(t + 60)}"
                        found_options.append(
                            f"{day_label}: {COURTS[c1]} -> {COURTS[c2]} at {t_mid} ({time_range})"
                        )

    return found_options


def main():
    ottawa_tz = ZoneInfo("America/Toronto")
    now_ottawa = datetime.now(ottawa_tz)

    target_days = [
        now_ottawa + timedelta(days=offset)
        for offset in range(3)
        if (now_ottawa + timedelta(days=offset)).weekday() in SCHEDULE_WINDOWS
    ]

    if not target_days:
        print("None of the next 3 days are Fri/Sat/Sun. Skipping check.")
        return

    all_available = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        stealth_sync(page)

        # 1. Open login page and wait past any Vercel security checkpoint
        page.goto(f"{BASE_URL}/", wait_until="domcontentloaded")
        try:
            email_box = page.locator("input[name='email'], input[id='email'], input[type='text']").first
            email_box.wait_for(state="visible", timeout=25000)
        except Exception:
            print("Timed out! Current page title:", page.title())
            print("Page snippet:", page.inner_text("body")[:300])
            raise

        # 2. Log in
        email_box.fill(CLUB_EMAIL)
        page.locator("input[name='password'], input[type='password']").first.fill(CLUB_PASSWORD)
        page.locator("button[type='submit'], input[type='submit'], #loginButton").first.click()
        page.wait_for_load_state("networkidle")
        print("Logged into Rideau Sports Centre via browser.")

        # 3. Open Reserve a Court page
        page.goto(f"{BASE_URL}/tennis/reserve-for-member", wait_until="networkidle")

        for target_dt in target_days:
            date_str = target_dt.strftime("%m/%d/%Y")
            html = page.evaluate(
                """async (dateStr) => {
                    const form = document.querySelector('#courtScheduleProperties');
                    const formData = new URLSearchParams(new FormData(form));
                    formData.set('date', dateStr);
                    formData.set('end_date', dateStr);
                    formData.set('locations', '1');
                    formData.set('types', 'all');
                    const resp = await fetch('/resource/court-schedule?ajax=true', {
                        method: 'POST',
                        headers: { 'X-Requested-With': 'XMLHttpRequest' },
                        body: formData
                    });
                    return await resp.text();
                }""",
                date_str,
            )
            day_matches = parse_grid_html(html, target_dt, now_ottawa)
            all_available.extend(day_matches)

        browser.close()

    print(f"Total matching 1-hr options found: {len(all_available)}")
    for opt in all_available:
        print(" -", opt)

    seen_courts = load_seen_courts()
    new_courts = [c for c in all_available if c not in seen_courts]

    if new_courts:
        message = "\n".join(new_courts)
        requests.post(
            f"https://ntfy.sh{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": f"RSC Tennis Open ({len(new_courts)} slot{'s' if len(new_courts) > 1 else ''})",
                "Priority": "high",
                "Tags": "tennis",
            },
        )
        print(f"Sent alert for {len(new_courts)} new option(s)!")
    else:
        print("No new court openings since last check.")

    save_seen_courts(all_available)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
