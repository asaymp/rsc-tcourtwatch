import os
import re
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
CLUB_EMAIL = os.environ.get("CLUB_EMAIL")
CLUB_PASSWORD = os.environ.get("CLUB_PASSWORD")

STATE_FILE = "seen_courts.json"
BASE_URL = "https://clubautomation.com"
PLAYER_ID = "58236"

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


def login(session):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"{BASE_URL}/",
    }
    resp = session.get(f"{BASE_URL}/", headers=headers)
    soup = BeautifulSoup(resp.text, "html.parser")
    token_input = soup.find("input", {"name": "login_token"})
    login_token = token_input["value"] if token_input else ""

    payload = {
        "email": CLUB_EMAIL,
        "password": CLUB_PASSWORD,
        "login_token": login_token,
    }
    login_resp = session.post(f"{BASE_URL}/login/login", data=payload, headers=headers)

    if "Invalid" in login_resp.text or "Login to your account" in login_resp.text:
        raise RuntimeError("Login failed! Double-check CLUB_EMAIL and CLUB_PASSWORD secrets.")
    print("Logged into Rideau Sports Centre successfully.")


def fetch_day_slots(session, target_dt, now_ottawa):
    date_str = target_dt.strftime("%m/%d/%Y")
    day_label = target_dt.strftime("%a %b %d")
    weekday = target_dt.weekday()

    if weekday not in SCHEDULE_WINDOWS:
        return []

    win_start_str, win_end_str = SCHEDULE_WINDOWS[weekday]
    win_start = time_to_minutes(win_start_str)
    win_end = time_to_minutes(win_end_str)

    # Load the member reservation page first to initialize session cookies/state
    init_resp = session.get(
        f"{BASE_URL}/tennis/reserve-for-member",
        params={"date": date_str, "locations": "1", "ajax": "true"},
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}/member",
        },
    )

    # Extract dynamic season value from the form if available
    init_soup = BeautifulSoup(init_resp.text, "html.parser")
    season_input = init_soup.find("input", {"name": "season"})
    season_val = season_input["value"] if season_input else "123"

    payload = {
        "multiselectdata": "",
        "season": season_val,
        "schedule_id": "",
        "courts_confirmed": "0",
        "event_type": "",
        "component_id": "2",
        "event_id": "",
        "player": "",
        "players_string": PLAYER_ID,
        "player_ghost_string": "",
        "save": "0",
        "cancel": "0",
        "id": "",
        "time_begin": "",
        "time_end": "",
        "resource_id": "",
        "time_changed": "0",
        "reserve_for": "member-court",
        "guest_bill_to_members": "0",
        "guest_fee_to_members": "0",
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
    }

    resp = session.post(
        f"{BASE_URL}/resource/court-schedule?ajax=true",
        data=payload,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}/tennis/reserve-for-member",
        },
    )

    soup = BeautifulSoup(resp.text, "html.parser")
    all_tds = soup.find_all("td", id=re.compile(r"^court_\d+_row_\d{2}:\d{2}$"))
    drag_tds = [td for td in all_tds if "start_drag" in td.get("onmousedown", "")]
    print(f"[{day_label}] HTML length={len(resp.text)}, total cells={len(all_tds)}, bookable cells={len(drag_tds)}")

    open_by_time = {}
    now_minutes = now_ottawa.hour * 60 + now_ottawa.minute
    is_today = target_dt.date() == now_ottawa.date()

    for td in drag_tds:
        match = re.match(r"^court_(\d+)_row_(\d{2}:\d{2})$", td["id"])
        if not match:
            continue

        court_id, time_str = match.groups()
        court_name = COURTS.get(court_id, f"Excluded Court ({court_id})")
        print(f"   Open 30-min slot: {court_name} at {time_str}")

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

    session = requests.Session()
    login(session)

    all_available = []
    for offset in range(3):
        target_dt = now_ottawa + timedelta(days=offset)
        day_matches = fetch_day_slots(session, target_dt, now_ottawa)
        all_available.extend(day_matches)

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
