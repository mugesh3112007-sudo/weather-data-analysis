"""
Varsha Grid — a real, minimal working version of a weather verification platform.

What's REAL in this file:
  - Live weather data is fetched from the free Open-Meteo API (no API key needed)
  - Reports are stored in a real SQLite database on disk (weathergrid.db)
  - Every submitted report is checked against that live data before being marked
    "verified" or "flagged" — this is genuine verification logic, not a mock.

What's simplified (clearly marked TODO, for you to extend later):
  - Social media ingestion (Twitter/Reddit) is NOT wired up yet — reports come
    from the web form only. Adding a scraper is the next real step.
  - Image duplicate detection (perceptual hashing) is NOT implemented yet.
  - Dust storms aren't classifiable from this particular free weather API.
"""

import sqlite3
import time
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

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
            created_at INTEGER NOT NULL
        )
    """)
    conn.commit()

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


# ---------------------------------------------------------------------------
# Startup + static frontend
# ---------------------------------------------------------------------------
init_db()

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
