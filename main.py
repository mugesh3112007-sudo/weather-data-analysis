"""
Weather Data Analysis — a real, working weather verification platform.

What's REAL in this file:
  - Live weather data is fetched from the free Open-Meteo API (no API key needed)
  - Real Reddit posts are ingested automatically every 10 minutes, classified
    by keyword, and verified against live weather data (see ingest_reddit())
  - Real NASA EONET satellite-observed events (storms, floods, heatwaves,
    dust/haze) are ingested every 15 minutes, matched to the nearest tracked
    city, and verified — no API key needed at all (see ingest_nasa_eonet())
  - Reports are stored in a real SQLite database on disk (weathergrid.db)
  - Every report — from the form, Reddit, or NASA — is checked against live
    weather data before being marked "verified" or "flagged".

What's simplified (clearly marked TODO, for you to extend later):
  - Twitter/X ingestion is NOT wired up yet (requires a paid API tier now)
  - Image duplicate detection (perceptual hashing) is NOT implemented yet
  - Reddit classification is keyword-based, not a trained ML model
  - NASA "dustHaze" events can't be cross-checked against Open-Meteo (it has
    no dust data), so they're stored as "pending" rather than auto-verified —
    an honest limitation, not a bug
"""

import math
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import praw
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv()  # reads a local .env file if present; does nothing on Render,
                # where you set these as real environment variables instead

REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.environ.get("REDDIT_USER_AGENT", "weather-data-analysis-bot/1.0")

DB_PATH = Path(__file__).parent / "weathergrid.db"
STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# Cities we track. Real latitude/longitude — used to query real weather data.
# TODO: add more cities, or load this list from a database table instead.
# ---------------------------------------------------------------------------
CITIES = [
    {"id": "delhi",       "name": "Delhi",       "lat": 28.6139, "lon": 77.2090},
    {"id": "jaipur",      "name": "Jaipur",      "lat": 26.9124, "lon": 75.7873},
    {"id": "mumbai",      "name": "Mumbai",      "lat": 19.0760, "lon": 72.8777},
    {"id": "kolkata",     "name": "Kolkata",     "lat": 22.5726, "lon": 88.3639},
    {"id": "guwahati",    "name": "Guwahati",    "lat": 26.1445, "lon": 91.7362},
    {"id": "patna",       "name": "Patna",       "lat": 25.5941, "lon": 85.1376},
    {"id": "bengaluru",   "name": "Bengaluru",   "lat": 12.9716, "lon": 77.5946},
    {"id": "chennai",     "name": "Chennai",     "lat": 13.0827, "lon": 80.2707},
    {"id": "kochi",       "name": "Kochi",       "lat": 9.9312,  "lon": 76.2673},
    {"id": "bhubaneswar", "name": "Bhubaneswar", "lat": 20.2961, "lon": 85.8245},
    {"id": "hyderabad",   "name": "Hyderabad",   "lat": 17.3850, "lon": 78.4867},
]

EVENT_LABELS = {
    "flood":    "Flood",
    "storm":    "Thunderstorm / heavy rain",
    "heatwave": "Heatwave",
    "fog":      "Fog",
    "dust":     "Dust storm / haze",
    "clear":    "Clear conditions",
}

app = FastAPI(title="Weather Data Analysis API")

# CORS: your Vercel-hosted frontend lives on a different domain from your
# Render-hosted backend, so the browser needs explicit permission to call
# this API from there. "*" is fine for a hackathon demo; for anything more
# serious, replace it with your exact Vercel URL.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Database helpers (SQLite — a single file on disk, no server to install)
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            city_id TEXT NOT NULL,
            type TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL,
            flag_reason TEXT,
            ground_condition TEXT,
            created_at INTEGER NOT NULL,
            external_id TEXT
        )
    """)
    conn.commit()

    # Migration safety net: if you're running this against an older database
    # file created before "external_id" existed, add it now. Harmless no-op
    # on a fresh database (the column already exists from CREATE TABLE above).
    try:
        conn.execute("ALTER TABLE reports ADD COLUMN external_id TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Seed a few example reports on first run so the feed isn't empty.
    count = conn.execute("SELECT COUNT(*) AS c FROM reports").fetchone()["c"]
    if count == 0:
        seed = [
            ("Citizen App", "mumbai",   "flood", "Water logging near Andheri subway, traffic stalled since morning."),
            ("Reddit",      "kolkata",  "storm", "Heavy thunderstorm knocked out power in Salt Lake sector."),
            ("Citizen App", "chennai",  "storm", "Strong winds uprooted trees along OMR."),
        ]
        for s in seed:
            conn.execute(
                "INSERT INTO reports (source, city_id, type, description, status, flag_reason, ground_condition, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (s[0], s[1], s[2], s[3], "pending", None, None, int(time.time())),
            )
        conn.commit()
    conn.close()


def city_by_id(city_id):
    for c in CITIES:
        if c["id"] == city_id:
            return c
    return None


# ---------------------------------------------------------------------------
# REAL weather lookup — calls Open-Meteo's live forecast API.
# Docs: https://open-meteo.com/en/docs
# ---------------------------------------------------------------------------
def fetch_ground_truth(city_id: str):
    city = city_by_id(city_id)
    if not city:
        raise HTTPException(status_code=404, detail="Unknown city")

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={city['lat']}&longitude={city['lon']}"
        "&current_weather=true&hourly=precipitation&timezone=auto"
    )
    try:
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Weather API error: {e}")

    current = data.get("current_weather", {})
    weathercode = current.get("weathercode")
    temperature = current.get("temperature")

    # Pull the precipitation value for the current hour, if available.
    precip_now = None
    try:
        hourly_times = data["hourly"]["time"]
        hourly_precip = data["hourly"]["precipitation"]
        current_time = current.get("time")
        if current_time in hourly_times:
            idx = hourly_times.index(current_time)
            precip_now = hourly_precip[idx]
    except Exception:
        precip_now = None

    condition = classify_condition(weathercode, temperature, precip_now)

    return {
        "city_id": city_id,
        "weathercode": weathercode,
        "temperature_c": temperature,
        "precipitation_mm": precip_now,
        "condition": condition,
        "condition_label": EVENT_LABELS.get(condition, condition),
    }


def classify_condition(weathercode, temperature, precip_mm):
    """
    Turns Open-Meteo's raw weather code + temperature + rainfall into one of
    our event categories. This is intentionally simple, rule-based logic —
    a good place to plug in a trained ML classifier later.

    WMO weather codes reference: https://open-meteo.com/en/docs
    """
    if temperature is not None and temperature >= 40:
        return "heatwave"
    if weathercode in (45, 48):
        return "fog"
    if weathercode in (95, 96, 99):
        return "storm"
    if weathercode in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        if precip_mm is not None and precip_mm >= 15:
            return "flood"
        return "storm"
    return "clear"


# ---------------------------------------------------------------------------
# REAL Reddit ingestion — pulls recent posts mentioning each tracked city,
# classifies them by keyword, and verifies them against live weather data
# using the exact same fetch_ground_truth() logic as citizen reports.
#
# Requires free Reddit API credentials — see README section "Adding Reddit
# ingestion" for how to get REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET.
# ---------------------------------------------------------------------------
EVENT_KEYWORDS = {
    "flood":    ["flood", "flooding", "flooded", "waterlogged", "water logging", "inundat"],
    "storm":    ["storm", "thunderstorm", "cyclone", "heavy rain", "lightning", "gale", "downpour"],
    "heatwave": ["heatwave", "heat wave", "scorching", "record heat", "extreme heat"],
    "fog":      ["fog", "smog", "low visibility", "foggy"],
}


def classify_text(text: str):
    text_l = (text or "").lower()
    for event_type, keywords in EVENT_KEYWORDS.items():
        for kw in keywords:
            if kw in text_l:
                return event_type
    return None


def get_reddit_client():
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
        return None
    return praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )


def ingest_reddit():
    """
    Polls Reddit for each tracked city, pulls recent posts, classifies them,
    verifies them against live weather data, and stores new ones. Safe to
    call repeatedly — already-ingested posts (tracked by external_id) are
    skipped, so this can run on a schedule without creating duplicates.
    """
    reddit = get_reddit_client()
    if reddit is None:
        print("[reddit] Skipped — REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set.")
        return

    conn = get_db()
    total_added = 0

    for city in CITIES:
        query = f"{city['name']} weather"
        try:
            for submission in reddit.subreddit("all").search(query, sort="new", time_filter="day", limit=8):
                external_id = f"reddit:{submission.id}"

                existing = conn.execute(
                    "SELECT id FROM reports WHERE external_id = ?", (external_id,)
                ).fetchone()
                if existing:
                    continue

                text = f"{submission.title} {getattr(submission, 'selftext', '')}"
                event_type = classify_text(text)
                if not event_type:
                    continue  # not clearly about a weather event — skip it

                ground = fetch_ground_truth(city["id"])
                matches = ground["condition"] == event_type
                status = "verified" if matches else "flagged"
                flag_reason = None if matches else "mismatch"

                conn.execute(
                    "INSERT INTO reports "
                    "(source, city_id, type, description, status, flag_reason, ground_condition, created_at, external_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    ("Reddit", city["id"], event_type, submission.title[:280], status, flag_reason,
                     ground["condition"], int(time.time()), external_id),
                )
                conn.commit()
                total_added += 1
        except Exception as e:
            # A single city/subreddit failing shouldn't stop the whole run.
            print(f"[reddit] Error fetching posts for {city['id']}: {e}")

    conn.close()
    print(f"[reddit] Ingestion run complete — {total_added} new report(s) added.")


# ---------------------------------------------------------------------------
# REAL NASA EONET ingestion — pulls live, satellite/agency-observed natural
# events (severe storms, floods, extreme temperatures, dust/haze) with zero
# authentication required. Docs: https://eonet.gsfc.nasa.gov/docs/v3
#
# Each event is matched to whichever tracked city is nearest its most recent
# location, and — where our weather API can check it — verified the same way
# as every other source in this app.
# ---------------------------------------------------------------------------
NASA_CATEGORY_MAP = {
    "severeStorms": "storm",
    "floods":       "flood",
    "tempExtremes": "heatwave",  # EONET doesn't distinguish heat vs. cold extremes
    "dustHaze":     "dust",
}

NASA_MAX_DISTANCE_KM = 500  # how close an event must be to a tracked city to count


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two lat/lon points, in kilometers."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_city(lat, lon):
    """Returns (city, distance_km) for whichever tracked city is closest."""
    best_city, best_dist = None, None
    for city in CITIES:
        d = haversine_km(lat, lon, city["lat"], city["lon"])
        if best_dist is None or d < best_dist:
            best_city, best_dist = city, d
    return best_city, best_dist


def ingest_nasa_eonet():
    """
    Pulls currently-open NASA EONET events in our weather-relevant categories,
    matches each to the nearest tracked city (skipping anything too far from
    all of them), and stores new ones — verified against live weather data
    where possible, "pending" where our weather API simply can't check it
    (dust/haze has no equivalent in Open-Meteo).
    """
    categories = ",".join(NASA_CATEGORY_MAP.keys())
    url = f"https://eonet.gsfc.nasa.gov/api/v3/events?category={categories}&status=open&limit=50"

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[nasa] Error fetching EONET events: {e}")
        return

    events = data.get("events", [])
    conn = get_db()
    total_added = 0

    for event in events:
        event_categories = event.get("categories", [])
        if not event_categories:
            continue
        event_type = NASA_CATEGORY_MAP.get(event_categories[0].get("id"))
        if not event_type:
            continue

        geometry = event.get("geometry", [])
        if not geometry:
            continue
        coords = geometry[-1].get("coordinates")  # most recent point
        if not coords or len(coords) < 2:
            continue
        event_lon, event_lat = coords[0], coords[1]

        city, distance_km = nearest_city(event_lat, event_lon)
        if city is None or distance_km > NASA_MAX_DISTANCE_KM:
            continue  # not near any city we track — skip it

        external_id = f"nasa:{event['id']}"
        existing = conn.execute(
            "SELECT id FROM reports WHERE external_id = ?", (external_id,)
        ).fetchone()
        if existing:
            continue

        description = event.get("title", "Untitled event")
        if event.get("description"):
            description += f" — {event['description']}"

        if event_type == "dust":
            # Open-Meteo has no dust/haze signal to check this against —
            # store it as pending rather than falsely claiming verification.
            status, flag_reason, ground_condition = "pending", None, None
        else:
            ground = fetch_ground_truth(city["id"])
            matches = ground["condition"] == event_type
            status = "verified" if matches else "flagged"
            flag_reason = None if matches else "mismatch"
            ground_condition = ground["condition"]

        conn.execute(
            "INSERT INTO reports "
            "(source, city_id, type, description, status, flag_reason, ground_condition, created_at, external_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("NASA EONET", city["id"], event_type, description[:280], status, flag_reason,
             ground_condition, int(time.time()), external_id),
        )
        conn.commit()
        total_added += 1

    conn.close()
    print(f"[nasa] Ingestion run complete — {total_added} new report(s) added.")


# ---------------------------------------------------------------------------
# API models & routes
# ---------------------------------------------------------------------------
class ReportIn(BaseModel):
    city_id: str
    type: str
    description: str
    source: str = "Citizen App"


@app.get("/api/cities")
def get_cities():
    return CITIES


@app.get("/api/source-status")
def get_source_status():
    """
    Reports which data sources are actually active right now, so the
    frontend can show a true "Active" / "Not configured" state instead of
    just listing every source as if it were contributing data.
    """
    return {
        "sources": [
            {
                "id": "open-meteo",
                "name": "Live weather ground-truth data",
                "detail": "Open-Meteo API — open-meteo.com",
                "url": "https://open-meteo.com",
                "active": True,
            },
            {
                "id": "nasa-eonet",
                "name": "Satellite-observed event data",
                "detail": "NASA EONET API — eonet.gsfc.nasa.gov",
                "url": "https://eonet.gsfc.nasa.gov",
                "active": True,
            },
            {
                "id": "reddit",
                "name": "Social report ingestion",
                "detail": "Reddit API (PRAW) — praw.readthedocs.io",
                "url": "https://praw.readthedocs.io",
                "active": get_reddit_client() is not None,
            },
        ]
    }


@app.get("/api/ground-truth/{city_id}")
def get_ground_truth(city_id: str):
    return fetch_ground_truth(city_id)


@app.get("/api/reports")
def get_reports():
    conn = get_db()
    rows = conn.execute("SELECT * FROM reports ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/reports")
def create_report(report: ReportIn):
    if not city_by_id(report.city_id):
        raise HTTPException(status_code=400, detail="Unknown city")
    if report.type not in EVENT_LABELS:
        raise HTTPException(status_code=400, detail="Unknown event type")

    ground = fetch_ground_truth(report.city_id)
    matches = ground["condition"] == report.type
    status = "verified" if matches else "flagged"
    flag_reason = None if matches else "mismatch"

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO reports (source, city_id, type, description, status, flag_reason, ground_condition, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (report.source, report.city_id, report.type, report.description, status, flag_reason,
         ground["condition"], int(time.time())),
    )
    conn.commit()
    new_id = cur.lastrowid
    row = conn.execute("SELECT * FROM reports WHERE id=?", (new_id,)).fetchone()
    conn.close()

    return {"report": dict(row), "ground_truth": ground, "matches": matches}


@app.post("/api/ingest/reddit")
def trigger_reddit_ingest():
    """Manually trigger a Reddit ingestion run right now — useful for demos,
    so you don't have to wait for the scheduled 10-minute interval."""
    if get_reddit_client() is None:
        raise HTTPException(
            status_code=503,
            detail="Reddit credentials not configured. Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET.",
        )
    ingest_reddit()
    return {"status": "ingestion run complete"}


@app.post("/api/ingest/nasa")
def trigger_nasa_ingest():
    """Manually trigger a NASA EONET ingestion run right now — no credentials
    needed, so this always works."""
    ingest_nasa_eonet()
    return {"status": "ingestion run complete"}


# ---------------------------------------------------------------------------
# Startup + static frontend
# ---------------------------------------------------------------------------
init_db()

scheduler = BackgroundScheduler()
scheduler.add_job(ingest_reddit, "interval", minutes=10, next_run_time=datetime.now())
scheduler.add_job(ingest_nasa_eonet, "interval", minutes=15, next_run_time=datetime.now())
scheduler.start()

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
