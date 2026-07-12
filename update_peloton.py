#!/usr/bin/env python3
"""
update_peloton.py
Fetches new Peloton workouts, updates the CSV, rebuilds index.html, pushes to GitHub.
Run manually or via scheduled task daily at 8pm.
"""

import os
import json
import csv
import base64
import re
import time
import sys
from datetime import datetime

try:
    import requests
    from dotenv import load_dotenv
except ImportError:
    print("Missing dependencies. Run: pip3 install requests python-dotenv --break-system-packages")
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

GITHUB_TOKEN     = os.getenv('GITHUB_TOKEN')
GITHUB_REPO      = 'soroosj/peloton-dashboard'
GITHUB_FILE      = 'index.html'
GITHUB_CSV_FILE  = 'pedalingdata_workouts.csv'

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
CSV_PATH  = os.path.join(BASE_DIR, 'pedalingdata_workouts.csv')
HTML_PATH = os.path.join(BASE_DIR, 'index.html')
ENV_PATH  = os.path.join(BASE_DIR, '.env')

PELOTON_API    = 'https://api.onepeloton.com'
AUTH0_TOKEN_URL = 'https://auth.onepeloton.com/oauth/token'
AUTH0_CLIENT_ID = 'WVoJxVDdPoFx4RNewvvg6ch2mZ7bwnsM'

HEADERS = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
}

# ── Peloton Auth (Auth0 refresh token flow) ───────────────────────────────────
def peloton_login():
    refresh_token = os.getenv('PELOTON_REFRESH_TOKEN')
    if not refresh_token:
        raise ValueError("Missing PELOTON_REFRESH_TOKEN in .env")

    resp = requests.post(AUTH0_TOKEN_URL, json={
        'grant_type': 'refresh_token',
        'client_id': AUTH0_CLIENT_ID,
        'refresh_token': refresh_token,
    }, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()

    access_token = data['access_token']
    new_refresh   = data.get('refresh_token')

    # Save rotated refresh token back to .env
    if new_refresh and new_refresh != refresh_token:
        with open(ENV_PATH, 'r') as f:
            env_content = f.read()
        env_content = re.sub(r'PELOTON_REFRESH_TOKEN=.*', f'PELOTON_REFRESH_TOKEN={new_refresh}', env_content)
        with open(ENV_PATH, 'w') as f:
            f.write(env_content)

    # Get user_id
    me = requests.get(f'{PELOTON_API}/api/me',
                      headers={**HEADERS, 'Authorization': f'Bearer {access_token}'})
    me.raise_for_status()
    user_id = me.json()['id']
    print(f"  ✓ Authenticated (user: {me.json().get('username')})")
    return user_id, access_token

# ── Fetch Workouts ────────────────────────────────────────────────────────────
def get_workouts_since(user_id, token, since_dt):
    """Fetch workouts from API newer than since_dt. Returns list of workout dicts."""
    auth_headers = {**HEADERS, 'Authorization': f'Bearer {token}'}
    all_new = []
    page = 0
    limit = 100
    since_ts = since_dt.timestamp() if since_dt else 0

    while True:
        resp = requests.get(
            f'{PELOTON_API}/api/user/{user_id}/workouts',
            params={'limit': limit, 'page': page, 'sort_by': 'created_at', 'desc': 'true', 'joins': 'ride'},
            headers=auth_headers
        )
        resp.raise_for_status()
        data = resp.json()
        workouts = data.get('data', [])

        if not workouts:
            break

        found_old = False
        for w in workouts:
            created_at = w.get('created_at', 0)
            if created_at > since_ts:
                all_new.append(w)
            else:
                found_old = True

        if found_old:
            break

        total = data.get('total', 0)
        if len(all_new) >= total or len(workouts) < limit:
            break

        page += 1

    return all_new

def get_workout_details(workout_id, token):
    """Fetch full details for one workout (includes summaries with avg metrics)."""
    try:
        resp = requests.get(
            f'{PELOTON_API}/api/workout/{workout_id}',
            headers={**HEADERS, 'Authorization': f'Bearer {token}'}
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"    Warning: could not fetch details for {workout_id}: {e}")
    return {}

# ── CSV Helpers ───────────────────────────────────────────────────────────────
def get_latest_csv_datetime():
    """Return the most recent workout datetime from the CSV."""
    latest = None
    try:
        with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts_str = (row.get('Workout Timestamp') or '').strip()
                if not ts_str:
                    continue
                try:
                    dt = datetime.strptime(ts_str[:16], '%Y-%m-%d %H:%M')
                    if latest is None or dt > latest:
                        latest = dt
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return latest

def extract_type_from_title(title):
    """Best-effort: extract class type from title like '30 min Music Ride' -> 'Music'."""
    if not title:
        return ''
    # Pattern: "{N} min {TYPE} {WORD}" where last word is discipline keyword
    m = re.match(
        r'^\d+\s+min\s+(.*?)\s+(?:Ride|Run|Walk|Workout|Strength|Class|Meditation|Yoga|Stretch|Row|Bike)$',
        title.strip(), re.IGNORECASE
    )
    if m:
        return m.group(1).strip()
    return ''

def parse_summaries(summaries):
    """Extract metric values from Peloton summaries array."""
    result = {}
    for s in (summaries or []):
        slug = (s.get('slug') or '').lower()
        val = s.get('value')
        if val is None:
            continue
        result[slug] = val
    return result

def workout_to_csv_row(workout, details):
    """Map a Peloton API workout dict to a CSV row matching our format."""
    ride = workout.get('ride') or {}
    instructor_obj = ride.get('instructor') or {}
    instructor = instructor_obj.get('name', '')

    created_at = workout.get('created_at', 0)
    dt = datetime.fromtimestamp(created_at)
    ts_str = dt.strftime('%Y-%m-%d %H:%M (%Z)')

    duration_sec = ride.get('duration', 0) or 0
    duration_min = duration_sec // 60

    # Normalize discipline
    discipline_raw = (workout.get('fitness_discipline') or '').replace('_', ' ')
    discipline = discipline_raw.title()

    title = ride.get('title', '') or ''
    workout_type = extract_type_from_title(title)

    total_work = workout.get('total_work') or 0
    total_output = round(total_work / 1000) if total_work else ''

    # Get averages from summaries in details
    summaries = parse_summaries((details or {}).get('summaries', []))

    def sv(key, alt_keys=None):
        v = summaries.get(key)
        if v is None and alt_keys:
            for k in alt_keys:
                v = summaries.get(k)
                if v is not None:
                    break
        return str(round(float(v), 2)) if v is not None else ''

    avg_watts      = sv('avg_watts', ['average_watts'])
    avg_resistance = str(round(float(summaries['avg_resistance_perc']))) if 'avg_resistance_perc' in summaries else (
                     str(round(float(summaries['avg_resistance']))) if 'avg_resistance' in summaries else '')
    resistance_fmt = f'{avg_resistance}%' if avg_resistance else ''
    avg_cadence    = sv('avg_cadence_rpm', ['avg_cadence'])
    avg_speed      = sv('avg_speed')
    distance       = sv('distance')
    calories       = sv('calories')
    avg_hr         = sv('avg_heart_rate', ['avg_heartrate'])

    return [
        ts_str,           # Workout Timestamp
        'On Demand',      # Live/On-Demand
        instructor,       # Instructor Name
        str(duration_min),# Length (minutes)
        discipline,       # Fitness Discipline
        workout_type,     # Type
        title,            # Title
        '',               # Class Timestamp
        str(total_output),# Total Output
        avg_watts,        # Avg. Watts
        resistance_fmt,   # Avg. Resistance
        avg_cadence,      # Avg. Cadence (RPM)
        avg_speed,        # Avg. Speed (kph)
        distance,         # Distance (km)
        calories,         # Calories Burned
        avg_hr,           # Avg. Heartrate
        '',               # Avg. Incline
        '',               # Avg. Pace (min/km)
    ]

def append_to_csv(rows):
    """Append new rows to the CSV file."""
    with open(CSV_PATH, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)

# ── HTML Rebuild ──────────────────────────────────────────────────────────────
def rebuild_html():
    """Re-read CSV and inject fresh RAW JSON into index.html."""
    rows = []
    disciplines, instructors, types, lengths = set(), set(), set(), set()

    def to_float(val):
        s = str(val).strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = (row.get('Workout Timestamp') or '').strip()
            if not ts:
                continue
            date_str = ts[:10]

            discipline   = (row.get('Fitness Discipline') or '').strip()
            instructor   = (row.get('Instructor Name') or '').strip()
            length_str   = (row.get('Length (minutes)') or '').strip()
            length       = int(length_str) if length_str.isdigit() else None
            workout_type = (row.get('Type') or '').strip()
            title        = (row.get('Title') or '').strip()
            total_output = to_float(row.get('Total Output'))
            avg_watts    = to_float(row.get('Avg. Watts'))

            res_raw = str(row.get('Avg. Resistance') or '').strip().replace('%', '')
            avg_resistance = res_raw if res_raw else None

            avg_cadence = to_float(row.get('Avg. Cadence (RPM)'))
            distance    = to_float(row.get('Distance (km)'))
            calories    = to_float(row.get('Calories Burned'))
            avg_hr      = to_float(row.get('Avg. Heartrate'))

            rows.append([
                date_str, discipline, instructor, length,
                workout_type, title, total_output, avg_watts,
                avg_resistance, avg_cadence, distance, calories, avg_hr
            ])

            if discipline:   disciplines.add(discipline)
            if instructor:   instructors.add(instructor)
            if workout_type: types.add(workout_type)
            if length:       lengths.add(length)

    raw = {
        'rows':        rows,
        'disciplines': sorted(disciplines),
        'instructors': sorted(instructors),
        'types':       sorted(types),
        'lengths':     sorted(lengths),
    }
    raw_json = json.dumps(raw, separators=(',', ':'))

    # Always fetch current template from GitHub so local copy stays in sync
    gh_headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
    }
    gh_url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}'
    gh_resp = requests.get(gh_url, headers=gh_headers)
    if gh_resp.ok:
        html = base64.b64decode(gh_resp.json()['content']).decode('utf-8')
    else:
        with open(HTML_PATH, 'r', encoding='utf-8') as f:
            html = f.read()

    start = html.find('const RAW = ')
    if start == -1:
        raise ValueError("Could not find 'const RAW = ' in index.html")

    # Find matching closing brace
    pos = start + len('const RAW = ')
    depth, end = 0, pos
    for i in range(pos, len(html)):
        if html[i] == '{':
            depth += 1
        elif html[i] == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    # Skip trailing semicolon
    semi = end
    while semi < len(html) and html[semi] in (' ', '\t'):
        semi += 1
    if semi < len(html) and html[semi] == ';':
        semi += 1

    new_html = html[:start] + f'const RAW = {raw_json};' + html[semi:]

    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(new_html)

    print(f"  ✓ Rebuilt index.html with {len(rows)} workouts")
    return len(rows)

# ── GitHub Push ───────────────────────────────────────────────────────────────
def push_file_to_github(local_path, remote_path, commit_message):
    """Push a single file to GitHub via the Contents API (create/update)."""
    with open(local_path, 'rb') as f:
        content_b64 = base64.b64encode(f.read()).decode()

    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
    }
    url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{remote_path}'

    # Get current file SHA (required for update; absent if file doesn't exist yet)
    r = requests.get(url, headers=headers)
    sha = r.json()['sha'] if r.status_code == 200 else None

    payload = {'message': commit_message, 'content': content_b64}
    if sha:
        payload['sha'] = sha

    r = requests.put(url, headers=headers, json=payload)
    r.raise_for_status()
    print(f"  ✓ Pushed to GitHub: {GITHUB_REPO}/{remote_path}")

def push_to_github():
    """Push index.html and the workouts CSV to GitHub via API."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    push_file_to_github(HTML_PATH, GITHUB_FILE, f'Auto-update dashboard {now}')
    push_file_to_github(CSV_PATH, GITHUB_CSV_FILE, f'Auto-update CSV {now}')

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*55}")
    print(f"  Peloton Dashboard Update  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}")

    # Validate env
    missing = [v for v in ('PELOTON_REFRESH_TOKEN', 'GITHUB_TOKEN') if not os.getenv(v)]
    if missing:
        print(f"ERROR: Missing .env variables: {', '.join(missing)}")
        sys.exit(1)

    # Step 1: Find latest workout in CSV
    latest_dt = get_latest_csv_datetime()
    print(f"\n[1/5] Latest CSV workout: {latest_dt or 'none (full sync)'}")

    # Step 2: Authenticate
    print(f"\n[2/5] Authenticating with Peloton...")
    user_id, token = peloton_login()

    # Step 3: Fetch new workouts
    print(f"\n[3/5] Fetching new workouts from API...")
    new_workouts = get_workouts_since(user_id, token, latest_dt)
    print(f"  Found {len(new_workouts)} new workout(s)")

    if not new_workouts:
        print("\n  Dashboard is already up to date.")
        return

    # Step 4: Fetch details and append to CSV
    print(f"\n[4/5] Fetching workout details and updating CSV...")
    csv_rows = []
    for i, w in enumerate(reversed(new_workouts)):  # oldest first
        if w.get('status') not in (None, '', 'COMPLETE'):
            print(f"  Skipping incomplete workout ({w.get('status')})")
            continue
        title = (w.get('ride') or {}).get('title', 'Unknown')
        print(f"  [{i+1}/{len(new_workouts)}] {title}")
        details = get_workout_details(w['id'], token)
        time.sleep(0.2)  # be polite to the API
        csv_rows.append(workout_to_csv_row(w, details))

    append_to_csv(csv_rows)
    print(f"  ✓ Appended {len(csv_rows)} row(s) to CSV")

    # Step 5: Rebuild HTML and push
    in_actions = os.getenv('GITHUB_ACTIONS') == 'true'
    print(f"\n[5/5] Rebuilding dashboard{'...' if in_actions else ' and pushing to GitHub...'}")
    total = rebuild_html()
    if in_actions:
        print(f"  ✓ HTML rebuilt (GitHub Actions will commit & push)")
    else:
        push_to_github()

    print(f"\n{'='*55}")
    print(f"  ✅ Done! {len(csv_rows)} new workout(s) added ({total} total)")
    print(f"  Dashboard: https://soroosj.github.io/peloton-dashboard")
    print(f"{'='*55}\n")

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(0)  # Always exit 0 — suppress macOS launchd notifications
