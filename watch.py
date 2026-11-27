import os
import re
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

# Load local .env file from your phone
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
CLUB_EMAIL = os.environ.get("CLUB_EMAIL")
CLUB_PASSWORD = os.environ.get("CLUB_PASSWORD")

STATE_FILE = "seen_courts.json"
BASE_URL = "https://clubautomation.com"
PLAYER_ID = "58236"
CHECK_INTERVAL_SECONDS = 120  # Check every 2 minutes

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
    print(f"  [{day_label}] cells={len(all_tds)}, open_slots={len(drag_tds)}")
    if len(all_tds) == 0:
        print("     DEBUG HTML:", BeautifulSoup(html, "html.parser").get_text(" ", strip=True)[:220])
    for td in drag_tds:
        print(f"     -> {td['id']}")
        



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
def run_check():
    ottawa_tz = ZoneInfo("America/Toronto")
    now_ottawa = datetime.now(ottawa_tz)
    stamp = now_ottawa.strftime("%I:%M:%S %p")

    target_days = [
        now_ottawa + timedelta(days=offset)
        for offset in range(3)
        if (now_ottawa + timedelta(days=offset)).weekday() in SCHEDULE_WINDOWS
    ]

    if not target_days:
        print(f"[{stamp}] Next 3 days are Mon-Thu. Sleeping...")
        return

    s = cffi_requests.Session(impersonate="chrome")
    resp = s.get(f"{BASE_URL}/", timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")

    # Find the actual login form on the page and grab all hidden fields + action URL
    login_form = soup.find("form")
    login_action = f"{BASE_URL}/login/login"
    login_data = {}
    if login_form:
        if login_form.get("action"):
            act = login_form["action"]
            login_action = act if act.startswith("http") else f"{BASE_URL}{act if act.startswith('/') else '/' + act}"
        for inp in login_form.find_all("input"):
            name = inp.get("name")
            if name:
                login_data[name] = inp.get("value", "")

    # Populate username/email & password into whichever field names the form expects
    for key in list(login_data.keys()):
        if "user" in key.lower() or "email" in key.lower() or "login" == key.lower():
            login_data[key] = CLUB_EMAIL
        elif "pass" in key.lower():
            login_data[key] = CLUB_PASSWORD
    if not any("pass" in k.lower() for k in login_data):
        login_data["email"] = CLUB_EMAIL
        login_data["password"] = CLUB_PASSWORD

    print("Submitting login to:", login_action, "| Fields:", list(login_data.keys()))
    login_resp = s.post(
        login_action,
        data=login_data,
        headers={"Referer": f"{BASE_URL}/"},
        timeout=20,
    )
    print("Login status:", login_resp.status_code, "| Landed URL:", login_resp.url)
    print("Login snippet:", BeautifulSoup(login_resp.text, "html.parser").get_text(" ", strip=True)[:150])

    if "Login to your account" in login_resp.text or "Invalid" in login_resp.text:
        print(f"[{stamp}] ERROR: Login failed! Check .env credentials.")
        return

    
    print("Login status:", login_resp.status_code, "| Landed URL:", login_resp.url)
    print("Login snippet:", BeautifulSoup(login_resp.text, "html.parser").get_text(" ", strip=True)[:150])

    all_available = []
    for target_dt in target_days:
        date_str = target_dt.strftime("%m/%d/%Y")
        # Must include ajax=true so Club Automation initializes the member booking session
        init_resp = s.get(
            f"{BASE_URL}/tennis/reserve-for-member",
            params={"date": date_str, "locations": "1", "ajax": "true"},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE_URL}/member"},
            timeout=20,
        )
        init_soup = BeautifulSoup(init_resp.text, "html.parser")

        print("Init status:", init_resp.status_code, "| Form found:", bool(init_soup.find("form", {"id": "courtScheduleProperties"})))

        # Automatically grab all form fields from #courtScheduleProperties
        payload = {}
        form = init_soup.find("form", {"id": "courtScheduleProperties"})
        if form:
            for inp in form.find_all("input"):
                name = inp.get("name")
                if name:
                    payload[name] = inp.get("value", "")

        # Override with the exact booking search values
        payload.update({
            "players_string": PLAYER_ID,
            "reserve_for": "member-court",
            f"p_type_{PLAYER_ID}": "$",
            f"percent_{PLAYER_ID}": "",
            f"guest_bill_to_members[{PLAYER_ID}]": "0",
            f"guest_fee_to_members[{PLAYER_ID}]": "0",
            "rcUserName": "Add another Player",
            "rcUserPriceType": "$",
            "rcUserPrice": "-default-",
            "rcUserIsGuest": "1",
            "date": date_str,
            "end_date": date_str,
            "locations": "1",
            "types": "all",
        })

        grid_resp = s.post(
            f"{BASE_URL}/resource/court-schedule?ajax=true",
            data=payload,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE_URL}/member"},
            timeout=20,
        )
        all_available.extend(parse_grid_html(grid_resp.text, target_dt, now_ottawa))


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
            timeout=15,
        )
        print(f"[{stamp}] ALERT SENT for {len(new_courts)} opening(s):")
        for c in new_courts:
            print("  ->", c)
    else:
        print(f"[{stamp}] Checked {len(target_days)} day(s) — {len(all_available)} open total, 0 new.")

    save_seen_courts(all_available)


if __name__ == "__main__":
    print("Starting RSC Court Watcher (press Ctrl+C to stop)...")
    while True:
        try:
            run_check()
        except Exception as e:
            print(f"Error during check: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)
