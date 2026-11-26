import os
import json
import requests

# Pull your secret topic name safely from GitHub Secrets
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
STATE_FILE = "seen_courts.json"

def load_seen_courts():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return []

def save_seen_courts(courts):
    with open(STATE_FILE, "w") as f:
        json.dump(courts, f)

def check_club_website():
    # --- REPLACE THIS SECTION WITH YOUR CLUB'S LOGIC ---
    # This is dummy data so you can test the workflow right now.
    # Later, you will replace this with requests.get() to your club's URL.
    current_open_slots = ["Court 1 - Tomorrow 18:00"]
    # ---------------------------------------------------
    return current_open_slots

def main():
    seen_courts = load_seen_courts()
    open_courts = check_club_website()

    # Find only courts that weren't open the last time we checked
    new_courts = [court for court in open_courts if court not in seen_courts]

    if new_courts:
        message = "New court(s) opened up:\n" + "\n".join(new_courts)
        requests.post(
            f"https://ntfy.sh{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": "Tennis Court Alert!",
                "Priority": "high",
                "Tags": "tennis"
            }
        )
        print(f"Sent alert for: {new_courts}")
    else:
        print("No new courts found.")

    # Update the memory file so we don't alert on these again
    save_seen_courts(open_courts)

if __name__ == "__main__":
    main()
