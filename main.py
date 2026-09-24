"""
Weather Data Analysis v2.0 — a real, working weather verification platform
with impact-based forecasting.

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
  - Impact-based risk scoring uses real weather parameters (temp, precip, wind)
  - 7-day forecasts are fetched live from Open-Meteo

What's simplified (clearly marked TODO, for you to extend later):
  - Twitter/X ingestion is NOT wired up yet (requires a paid API tier now)
  - Reddit classification is keyword-based, not a trained ML model
  - City vulnerability profiles are hardcoded (a real system would pull from
    census data and infrastructure databases)
  - NASA "dustHaze" events can't be cross-checked against Open-Meteo (it has
    no dust data), so they're stored as "pending" rather than auto-verified
"""

import io
import math
import os
import secrets
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import imagehash
import praw
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from PIL import Image

load_dotenv()  # reads a local .env file if present; does nothing on Render,
                # where you set these as real environment variables instead

REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.environ.get("REDDIT_USER_AGENT", "weather-data-analysis-bot/1.0")

DB_PATH = Path(__file__).parent / "weathergrid.db"
STATIC_DIR = Path(__file__).parent / "static"
UPLOADS_DIR = Path(__file__).parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

# How close two images' perceptual hashes must be (Hamming distance) to be
# treated as duplicates/recycled photos. Lower = stricter match required.
DUPLICATE_HASH_THRESHOLD = 6

# ---------------------------------------------------------------------------
# Cities we track. Real latitude/longitude — used to query real weather data.
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

# ---------------------------------------------------------------------------
# City vulnerability profiles — plausible data for Indian cities.
# A production system would pull this from census data + infrastructure DBs.
# ---------------------------------------------------------------------------
CITY_PROFILES = {
    "delhi": {
        "population": 32941000, "flood_prone": True, "coastal": False,
        "infra": {"Roads & Bridges": 2800, "Power Substations": 340,
                  "Hospitals": 180, "Schools": 5200, "Rail Lines": 150},
    },
    "mumbai": {
        "population": 21297000, "flood_prone": True, "coastal": True,
        "infra": {"Roads & Bridges": 2100, "Power Substations": 280,
                  "Hospitals": 220, "Schools": 4100, "Rail Lines": 95},
    },
    "kolkata": {
        "population": 15134000, "flood_prone": True, "coastal": False,
        "infra": {"Roads & Bridges": 1600, "Power Substations": 200,
                  "Hospitals": 140, "Schools": 3400, "Rail Lines": 110},
    },
    "chennai": {
        "population": 11235000, "flood_prone": True, "coastal": True,
        "infra": {"Roads & Bridges": 1400, "Power Substations": 180,
                  "Hospitals": 130, "Schools": 2800, "Rail Lines": 70},
    },
    "bengaluru": {
        "population": 13193000, "flood_prone": True, "coastal": False,
        "infra": {"Roads & Bridges": 1800, "Power Substations": 250,
                  "Hospitals": 160, "Schools": 3600, "Rail Lines": 45},
    },
    "hyderabad": {
        "population": 10534000, "flood_prone": False, "coastal": False,
        "infra": {"Roads & Bridges": 1500, "Power Substations": 210,
                  "Hospitals": 145, "Schools": 3100, "Rail Lines": 60},
    },
    "jaipur": {
        "population": 3975000, "flood_prone": False, "coastal": False,
        "infra": {"Roads & Bridges": 900, "Power Substations": 120,
                  "Hospitals": 80, "Schools": 1800, "Rail Lines": 40},
    },
    "guwahati": {
        "population": 1116000, "flood_prone": True, "coastal": False,
        "infra": {"Roads & Bridges": 450, "Power Substations": 60,
                  "Hospitals": 35, "Schools": 600, "Rail Lines": 25},
    },
    "patna": {
        "population": 2714000, "flood_prone": True, "coastal": False,
        "infra": {"Roads & Bridges": 650, "Power Substations": 85,
                  "Hospitals": 55, "Schools": 1200, "Rail Lines": 35},
    },
    "kochi": {
        "population": 2280000, "flood_prone": True, "coastal": True,
        "infra": {"Roads & Bridges": 550, "Power Substations": 75,
                  "Hospitals": 60, "Schools": 900, "Rail Lines": 20},
    },
    "bhubaneswar": {
        "population": 1136000, "flood_prone": True, "coastal": True,
        "infra": {"Roads & Bridges": 400, "Power Substations": 55,
                  "Hospitals": 40, "Schools": 650, "Rail Lines": 30},
    },
}

EVENT_LABELS = {
    "flood":    "Flood",
    "storm":    "Thunderstorm / heavy rain",
    "heatwave": "Heatwave",
    "fog":      "Fog",
    "dust":     "Dust storm / haze",
    "clear":    "Clear conditions",
}

app = FastAPI(title="Weather Data Analysis API", version="2.0.0")

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
# Simple in-memory rate limiter for report submission.
# Max 10 submissions per IP per 60 seconds.
# ---------------------------------------------------------------------------
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 10
_rate_limit_store = defaultdict(list)


def check_rate_limit(client_ip: str):
    """Raises HTTPException if the client has exceeded the submission rate limit."""
    now = time.time()
    # Clean old entries
    _rate_limit_store[client_ip] = [
        t for t in _rate_limit_store[client_ip]
        if now - t < RATE_LIMIT_WINDOW
    ]
    if len(_rate_limit_store[client_ip]) >= RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Max {RATE_LIMIT_MAX} submissions per minute.",
        )
    _rate_limit_store[client_ip].append(now)


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
            external_id TEXT,
            photo_path TEXT,
            photo_hash TEXT
        )
    """)
    conn.commit()

    # Migration safety net: if you're running this against an older database
    # file created before these columns existed, add them now. Harmless
    # no-op on a fresh database (columns already exist from CREATE TABLE above).
    for column, coltype in [("external_id", "TEXT"), ("photo_path", "TEXT"), ("photo_hash", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE reports ADD COLUMN {column} {coltype}")
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
# REAL perceptual image hashing — catches recycled/duplicate disaster photos.
# Uses imagehash's pHash algorithm: visually similar images (even resized,
# recompressed, or lightly cropped) produce hashes that are "close" to each
# other, measured by Hamming distance. This is a real, working version of
# the duplicate-detection idea from the original pitch — not a simulation.
#
# Known limitation, worth saying out loud in a demo: Render's free tier has
# an ephemeral filesystem, so uploaded photo files (not their hashes, which
# live in the database) are lost on every redeploy/restart. Fine for a demo;
# a production version would store images in S3 or similar cloud storage.
# ---------------------------------------------------------------------------
def compute_phash(image_bytes: bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    return imagehash.phash(img)


def find_duplicate(conn, new_hash):
    """Returns the id of an existing report whose photo hash is within the
    duplicate threshold of new_hash, or None if no match is found."""
    rows = conn.execute(
        "SELECT id, photo_hash FROM reports WHERE photo_hash IS NOT NULL"
    ).fetchall()
    for row in rows:
        try:
            existing_hash = imagehash.hex_to_hash(row["photo_hash"])
            if (new_hash - existing_hash) <= DUPLICATE_HASH_THRESHOLD:
                return row["id"]
        except Exception:
            continue
    return None


def save_uploaded_photo(filename: str, contents: bytes) -> str:
    """Saves the uploaded file to disk and returns its public URL path."""
    ext = Path(filename).suffix.lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".jpg"
    unique_name = f"{int(time.time())}_{secrets.token_hex(4)}{ext}"
    (UPLOADS_DIR / unique_name).write_bytes(contents)
    return f"/uploads/{unique_name}"


# ---------------------------------------------------------------------------
# REAL weather lookup — calls Open-Meteo's live forecast API.
# Enhanced in v2.0 to also return wind speed, humidity, and UV index.
# Docs: https://open-meteo.com/en/docs
# ---------------------------------------------------------------------------
def fetch_ground_truth(city_id: str):
    city = city_by_id(city_id)
    if not city:
        raise HTTPException(status_code=404, detail="Unknown city")

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={city['lat']}&longitude={city['lon']}"
        "&current_weather=true"
        "&hourly=precipitation,relative_humidity_2m,uv_index"
        "&timezone=auto"
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
    wind_speed = current.get("windspeed")

    # Pull hourly values for the current hour, if available.
    precip_now = None
    humidity_now = None
    uv_now = None
    try:
        hourly_times = data["hourly"]["time"]
        current_time = current.get("time")
        if current_time in hourly_times:
            idx = hourly_times.index(current_time)
            precip_now = data["hourly"]["precipitation"][idx]
            humidity_now = data["hourly"]["relative_humidity_2m"][idx]
            uv_now = data["hourly"]["uv_index"][idx]
    except Exception:
        pass

    condition = classify_condition(weathercode, temperature, precip_now)

    return {
        "city_id": city_id,
        "weathercode": weathercode,
        "temperature_c": temperature,
        "precipitation_mm": precip_now,
        "wind_speed_kmh": wind_speed,
        "humidity": humidity_now,
        "uv_index": uv_now,
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
# Impact-Based Forecasting Engine (v2.0)
# Computes risk scores, sector impacts, infrastructure vulnerability, early
# warnings, and population exposure — all driven by live weather data.
# ---------------------------------------------------------------------------

def compute_risk_score(ground):
    """Compute a 0-100 risk score from live weather parameters."""
    CONDITION_BASE = {
        "clear": 0, "fog": 15, "storm": 35, "flood": 55,
        "heatwave": 45, "dust": 20,
    }
    base = CONDITION_BASE.get(ground.get("condition", "clear"), 0)

    precip = ground.get("precipitation_mm") or 0
    precip_score = min(30, precip * 2)

    temp = ground.get("temperature_c") or 25
    if temp >= 40:
        temp_score = min(25, (temp - 35) * 4)
    elif temp <= 5:
        temp_score = min(15, (5 - temp) * 3)
    else:
        temp_score = 0

    wind = ground.get("wind_speed_kmh") or 0
    wind_score = min(20, max(0, (wind - 20) * 0.5))

    score = min(100, int(base + precip_score + temp_score + wind_score))
    return score


def risk_level(score):
    """Convert a numeric risk score to a categorical level."""
    if score >= 80:
        return "CRITICAL"
    if score >= 55:
        return "HIGH"
    if score >= 30:
        return "MODERATE"
    return "LOW"


# Sector impact templates keyed by weather condition.
# Each template has severity multipliers and condition-specific descriptions.
SECTOR_TEMPLATES = {
    "flood": [
        {"sector": "Transport", "icon": "🚗", "sev_mult": 1.0,
         "desc": "Road flooding likely on low-lying routes; waterlogging may strand vehicles and delay emergency response.",
         "advisory": "Avoid non-essential travel; use elevated routes where available"},
        {"sector": "Agriculture", "icon": "🌾", "sev_mult": 0.9,
         "desc": "Standing crops at risk of submersion; soil erosion accelerating in flood-prone agricultural belts.",
         "advisory": "Move livestock to higher ground; delay irrigation operations"},
        {"sector": "Health", "icon": "🏥", "sev_mult": 0.7,
         "desc": "Waterborne disease risk elevated; hospitals may face access issues in low-lying areas.",
         "advisory": "Prepare emergency medical supplies; boil water advisories may be needed"},
        {"sector": "Power Grid", "icon": "⚡", "sev_mult": 0.8,
         "desc": "Substation flooding could cause widespread outages; electrical hazards from exposed wiring.",
         "advisory": "Pre-position portable generators; avoid downed power lines"},
        {"sector": "Emergency Services", "icon": "🚨", "sev_mult": 1.0,
         "desc": "Search and rescue teams may be needed; evacuation routes should be pre-identified.",
         "advisory": "Activate flood emergency protocols; deploy rescue boats to staging areas"},
        {"sector": "Education", "icon": "🏫", "sev_mult": 0.5,
         "desc": "Schools in flood-prone zones may need to close; student safety is priority.",
         "advisory": "Consider preemptive school closures in affected areas"},
    ],
    "storm": [
        {"sector": "Transport", "icon": "🚗", "sev_mult": 0.8,
         "desc": "Reduced visibility and wet roads increase accident risk; flight delays likely.",
         "advisory": "Drive with headlights on; allow extra travel time"},
        {"sector": "Agriculture", "icon": "🌾", "sev_mult": 0.7,
         "desc": "Strong winds may damage standing crops; hail possible with severe thunderstorms.",
         "advisory": "Secure outdoor farm equipment; harvest ripe crops if possible"},
        {"sector": "Health", "icon": "🏥", "sev_mult": 0.4,
         "desc": "Lightning strike risk elevated; minor injuries from wind-blown debris possible.",
         "advisory": "Stay indoors during lightning; keep first aid supplies accessible"},
        {"sector": "Power Grid", "icon": "⚡", "sev_mult": 0.9,
         "desc": "High winds and lightning can cause outages; transformer damage possible.",
         "advisory": "Report downed lines immediately; avoid metal structures during lightning"},
        {"sector": "Emergency Services", "icon": "🚨", "sev_mult": 0.6,
         "desc": "Tree falls and debris clearance may require emergency response.",
         "advisory": "Pre-position chainsaws and debris clearance equipment"},
        {"sector": "Education", "icon": "🏫", "sev_mult": 0.3,
         "desc": "Outdoor school activities should be cancelled during severe weather warnings.",
         "advisory": "Move all activities indoors; review building safety protocols"},
    ],
    "heatwave": [
        {"sector": "Transport", "icon": "🚗", "sev_mult": 0.4,
         "desc": "Road surfaces may soften; rail tracks at risk of buckling in extreme heat.",
         "advisory": "Carry extra water and emergency supplies when traveling"},
        {"sector": "Agriculture", "icon": "🌾", "sev_mult": 1.0,
         "desc": "Severe crop stress and wilting; irrigation demand surging beyond capacity.",
         "advisory": "Implement emergency irrigation; shade sensitive crops if possible"},
        {"sector": "Health", "icon": "🏥", "sev_mult": 1.0,
         "desc": "Heatstroke risk critical for outdoor workers and elderly; hospital admissions expected to spike.",
         "advisory": "Open cooling shelters; distribute ORS packets; restrict outdoor labor 11AM-4PM"},
        {"sector": "Power Grid", "icon": "⚡", "sev_mult": 0.9,
         "desc": "AC load pushing grid to capacity; rolling blackouts possible in peak hours.",
         "advisory": "Reduce non-essential power consumption; pre-stage backup generators"},
        {"sector": "Emergency Services", "icon": "🚨", "sev_mult": 0.7,
         "desc": "Heat-related emergencies expected to increase; water distribution may be needed.",
         "advisory": "Deploy mobile water stations; alert hospitals for heat casualties"},
        {"sector": "Education", "icon": "🏫", "sev_mult": 0.6,
         "desc": "Schools without adequate cooling face unsafe conditions for students.",
         "advisory": "Reduce school hours; ensure drinking water availability"},
    ],
    "fog": [
        {"sector": "Transport", "icon": "🚗", "sev_mult": 1.0,
         "desc": "Near-zero visibility causing highway pileup risk; flight delays and diversions expected.",
         "advisory": "Use fog lights; maintain low speed; avoid overtaking"},
        {"sector": "Agriculture", "icon": "🌾", "sev_mult": 0.2,
         "desc": "Prolonged fog may encourage fungal diseases on crops.",
         "advisory": "Monitor crops for fungal infection signs"},
        {"sector": "Health", "icon": "🏥", "sev_mult": 0.5,
         "desc": "Respiratory issues may worsen, especially if fog traps pollutants.",
         "advisory": "Wear masks outdoors; avoid morning exercise in heavy fog"},
        {"sector": "Power Grid", "icon": "⚡", "sev_mult": 0.1,
         "desc": "Minimal direct impact on power infrastructure.",
         "advisory": "No special precautions needed"},
        {"sector": "Emergency Services", "icon": "🚨", "sev_mult": 0.6,
         "desc": "Response times may increase due to reduced visibility on roads.",
         "advisory": "Use GPS-assisted navigation; coordinate with traffic police"},
        {"sector": "Education", "icon": "🏫", "sev_mult": 0.4,
         "desc": "School bus routes may face delays; consider delayed start times.",
         "advisory": "Implement fog delay protocols for school transport"},
    ],
    "clear": [
        {"sector": "Transport", "icon": "🚗", "sev_mult": 0.0,
         "desc": "No weather-related transport disruptions expected.",
         "advisory": "Normal operations"},
        {"sector": "Agriculture", "icon": "🌾", "sev_mult": 0.0,
         "desc": "Favorable conditions for agricultural activities.",
         "advisory": "Normal farming operations"},
        {"sector": "Health", "icon": "🏥", "sev_mult": 0.0,
         "desc": "No weather-related health advisories.",
         "advisory": "Normal precautions apply"},
    ],
}


def generate_impacts(condition, risk_lvl):
    """Generate sector impact assessments based on weather condition and risk level."""
    templates = SECTOR_TEMPLATES.get(condition, SECTOR_TEMPLATES["clear"])
    SEV_MAP = {"LOW": 0, "MODERATE": 1, "HIGH": 2, "CRITICAL": 3}
    LEVELS = ["LOW", "MODERATE", "HIGH", "CRITICAL"]
    base_idx = SEV_MAP.get(risk_lvl, 0)

    impacts = []
    for tmpl in templates:
        adjusted_idx = min(3, int(base_idx * tmpl["sev_mult"]))
        if adjusted_idx == 0 and condition == "clear":
            # Skip sectors with zero impact in clear weather
            continue
        impacts.append({
            "sector": tmpl["sector"],
            "icon": tmpl["icon"],
            "severity": LEVELS[adjusted_idx],
            "description": tmpl["desc"],
            "advisory": tmpl["advisory"],
        })
    return impacts


def compute_infra_risk(city_id, condition, risk_score):
    """Compute infrastructure vulnerability based on city profile and weather."""
    profile = CITY_PROFILES.get(city_id)
    if not profile:
        return []

    # How much each infrastructure type is affected by each condition
    RELEVANCE = {
        "flood":    {"Roads & Bridges": 0.8, "Power Substations": 0.5, "Hospitals": 0.2, "Schools": 0.15, "Rail Lines": 0.6},
        "storm":    {"Roads & Bridges": 0.4, "Power Substations": 0.7, "Hospitals": 0.1, "Schools": 0.1, "Rail Lines": 0.3},
        "heatwave": {"Roads & Bridges": 0.1, "Power Substations": 0.6, "Hospitals": 0.3, "Schools": 0.2, "Rail Lines": 0.15},
        "fog":      {"Roads & Bridges": 0.3, "Power Substations": 0.05, "Hospitals": 0.05, "Schools": 0.1, "Rail Lines": 0.4},
        "dust":     {"Roads & Bridges": 0.2, "Power Substations": 0.3, "Hospitals": 0.1, "Schools": 0.15, "Rail Lines": 0.2},
        "clear":    {"Roads & Bridges": 0, "Power Substations": 0, "Hospitals": 0, "Schools": 0, "Rail Lines": 0},
    }

    relevance = RELEVANCE.get(condition, RELEVANCE["clear"])
    result = []
    score_factor = risk_score / 100.0

    for infra_type, total_count in profile["infra"].items():
        rel = relevance.get(infra_type, 0)
        count_at_risk = int(total_count * rel * score_factor)
        if count_at_risk > 0:
            item_risk = "CRITICAL" if rel * score_factor > 0.5 else (
                "HIGH" if rel * score_factor > 0.3 else (
                    "MODERATE" if rel * score_factor > 0.1 else "LOW"
                )
            )
            result.append({
                "type": infra_type,
                "count_at_risk": count_at_risk,
                "risk": item_risk,
            })

    # Sort by count_at_risk descending
    result.sort(key=lambda x: x["count_at_risk"], reverse=True)
    return result


def generate_early_warnings(city_id, ground):
    """Generate AI-style early warnings based on current weather conditions."""
    warnings = []
    profile = CITY_PROFILES.get(city_id, {})
    precip = ground.get("precipitation_mm") or 0
    temp = ground.get("temperature_c") or 25
    wind = ground.get("wind_speed_kmh") or 0
    humidity = ground.get("humidity") or 50
    condition = ground.get("condition", "clear")

    # Flash flood warning
    if precip > 10:
        confidence = min(95, 60 + int(precip * 2))
        warnings.append({
            "title": "Flash Flood Risk Rising",
            "severity": "CRITICAL" if precip > 25 else "HIGH",
            "confidence": confidence,
            "description": f"Sustained heavy rainfall of {precip}mm/hr detected. "
                          f"Urban drainage systems may be overwhelmed, particularly "
                          f"in low-lying areas. Waterlogging expected within 2-4 hours "
                          f"if rainfall continues at this intensity.",
        })
    elif precip > 5 and profile.get("flood_prone"):
        warnings.append({
            "title": "Flood Watch — Vulnerable Area",
            "severity": "MODERATE",
            "confidence": min(85, 50 + int(precip * 3)),
            "description": f"Moderate rainfall of {precip}mm/hr in a flood-prone region. "
                          f"River levels and storm drains should be monitored closely. "
                          f"Conditions could escalate if intensity increases.",
        })

    # Extreme heat
    if temp > 42:
        confidence = min(95, 70 + int((temp - 42) * 5))
        warnings.append({
            "title": "Extreme Heat Emergency",
            "severity": "CRITICAL",
            "confidence": confidence,
            "description": f"Temperature has reached {temp}°C — well above dangerous thresholds. "
                          f"Heat stroke risk is critical for outdoor workers, children, and elderly. "
                          f"Cooling shelters should be activated immediately.",
        })
    elif temp > 38:
        warnings.append({
            "title": "Heat Stress Advisory",
            "severity": "HIGH" if temp > 40 else "MODERATE",
            "confidence": min(90, 60 + int((temp - 38) * 5)),
            "description": f"Temperature at {temp}°C with humidity at {humidity}%. "
                          f"Heat index is elevated. Outdoor physical activity should be "
                          f"limited between 11 AM and 4 PM.",
        })

    # High wind
    if wind > 60:
        warnings.append({
            "title": "Severe Wind Warning",
            "severity": "CRITICAL",
            "confidence": min(95, 70 + int((wind - 60) * 0.5)),
            "description": f"Wind speeds of {wind} km/h detected — risk of structural damage, "
                          f"falling trees, and flying debris. Stay indoors and away from windows.",
        })
    elif wind > 40:
        warnings.append({
            "title": "High Wind Advisory",
            "severity": "HIGH" if wind > 50 else "MODERATE",
            "confidence": min(90, 55 + int((wind - 40) * 1.0)),
            "description": f"Wind speeds of {wind} km/h may cause damage to temporary structures, "
                          f"signage, and weak trees. Secure loose outdoor objects.",
        })

    # Fog / visibility
    if condition == "fog":
        warnings.append({
            "title": "Dense Fog — Visibility Alert",
            "severity": "MODERATE",
            "confidence": 75,
            "description": "Dense fog is reducing visibility significantly. "
                          "Highway and aviation operations are impacted. "
                          "Expect delays and exercise extreme caution while driving.",
        })

    # Coastal flooding risk
    if profile.get("coastal") and condition in ("storm", "flood"):
        warnings.append({
            "title": "Coastal Surge Risk",
            "severity": "HIGH",
            "confidence": 65,
            "description": f"Combined storm activity and coastal location increase tidal "
                          f"surge risk. Low-lying coastal areas should prepare for "
                          f"potential inundation during high tide windows.",
        })

    return warnings


def generate_alerts(ground, risk_lvl):
    """Generate active alert notices based on weather conditions and risk level."""
    alerts = []
    now = int(time.time())
    condition = ground.get("condition", "clear")
    precip = ground.get("precipitation_mm") or 0
    temp = ground.get("temperature_c") or 25

    if risk_lvl == "CRITICAL":
        alerts.append({
            "level": "CRITICAL",
            "message": f"CRITICAL weather alert: {ground.get('condition_label', condition)} "
                      f"conditions are severe. Immediate protective action recommended.",
            "issued_at": now - 1800,
            "valid_until": now + 21600,
        })
    elif risk_lvl == "HIGH":
        alerts.append({
            "level": "WARNING",
            "message": f"Weather WARNING: {ground.get('condition_label', condition)} "
                      f"conditions may cause significant disruption. Stay updated.",
            "issued_at": now - 3600,
            "valid_until": now + 14400,
        })

    if precip > 15:
        alerts.append({
            "level": "WARNING",
            "message": f"Heavy precipitation alert: {precip}mm/hr recorded. "
                      f"Flash flooding possible in low-lying and urban areas.",
            "issued_at": now - 900,
            "valid_until": now + 10800,
        })

    if temp > 42:
        alerts.append({
            "level": "CRITICAL",
            "message": f"Extreme heat alert: {temp}°C recorded. Heat stroke risk is "
                      f"very high. Avoid outdoor exposure between 10 AM and 5 PM.",
            "issued_at": now - 1200,
            "valid_until": now + 18000,
        })

    if condition in ("clear",) and risk_lvl == "LOW":
        alerts.append({
            "level": "ADVISORY",
            "message": "No significant weather hazards expected. Normal precautions apply.",
            "issued_at": now - 7200,
            "valid_until": now + 43200,
        })

    return alerts


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
async def create_report(
    request: Request,
    city_id: str = Form(...),
    type: str = Form(...),
    description: str = Form(...),
    source: str = Form("Citizen App"),
    photo: UploadFile = File(None),
):
    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(client_ip)

    if not city_by_id(city_id):
        raise HTTPException(status_code=400, detail="Unknown city")
    if type not in EVENT_LABELS:
        raise HTTPException(status_code=400, detail="Unknown event type")

    conn = get_db()

    photo_path = None
    photo_hash_str = None
    duplicate_of = None

    if photo is not None and photo.filename:
        contents = await photo.read()
        try:
            phash = compute_phash(contents)
            photo_hash_str = str(phash)
            duplicate_of = find_duplicate(conn, phash)
            photo_path = save_uploaded_photo(photo.filename, contents)
        except Exception as e:
            # A corrupt/unsupported image shouldn't crash the whole submission —
            # just proceed without photo analysis for this report.
            print(f"[photo] Could not process uploaded image: {e}")

    if duplicate_of is not None:
        # A recycled/duplicate photo overrides normal verification — even if
        # the claimed event type happens to match today's weather, reusing
        # someone else's image is itself the thing being flagged here.
        status = "flagged"
        flag_reason = "duplicate_image"
        ground_condition = None
        matches = False
        ground = None
    else:
        ground = fetch_ground_truth(city_id)
        matches = ground["condition"] == type
        status = "verified" if matches else "flagged"
        flag_reason = None if matches else "mismatch"
        ground_condition = ground["condition"]

    cur = conn.execute(
        "INSERT INTO reports "
        "(source, city_id, type, description, status, flag_reason, ground_condition, created_at, photo_path, photo_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (source, city_id, type, description, status, flag_reason, ground_condition,
         int(time.time()), photo_path, photo_hash_str),
    )
    conn.commit()
    new_id = cur.lastrowid
    row = conn.execute("SELECT * FROM reports WHERE id=?", (new_id,)).fetchone()
    conn.close()

    return {
        "report": dict(row),
        "ground_truth": ground,
        "matches": matches,
        "duplicate_of": duplicate_of,
    }


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
# Impact-Based Forecasting API (v2.0)
# ---------------------------------------------------------------------------
@app.get("/api/impact-forecast")
def get_impact_forecast_all():
    """National overview — risk scores for all tracked cities."""
    results = []
    for city in CITIES:
        try:
            ground = fetch_ground_truth(city["id"])
            score = compute_risk_score(ground)
            results.append({
                "city_id": city["id"],
                "city_name": city["name"],
                "risk_score": score,
                "risk_level": risk_level(score),
                "weather": {
                    "condition": ground["condition"],
                    "condition_label": ground["condition_label"],
                    "temperature_c": ground["temperature_c"],
                    "precipitation_mm": ground["precipitation_mm"],
                },
            })
        except Exception as e:
            print(f"[impact] Error computing impact for {city['id']}: {e}")
    return results


@app.get("/api/impact-forecast/{city_id}")
def get_impact_forecast_city(city_id: str):
    """Detailed impact assessment for a single city."""
    city = city_by_id(city_id)
    if not city:
        raise HTTPException(status_code=404, detail="Unknown city")

    ground = fetch_ground_truth(city_id)
    score = compute_risk_score(ground)
    lvl = risk_level(score)
    profile = CITY_PROFILES.get(city_id, {})

    # Population affected — estimate based on risk score and city population
    population = profile.get("population", 0)
    pop_factor = {
        "flood": 0.20, "storm": 0.10, "heatwave": 0.25,
        "fog": 0.05, "dust": 0.08, "clear": 0.0,
    }.get(ground["condition"], 0)
    pop_affected_est = int(population * (score / 100) * pop_factor)

    return {
        "city_id": city_id,
        "city_name": city["name"],
        "risk_score": score,
        "risk_level": lvl,
        "weather": {
            "condition": ground["condition"],
            "condition_label": ground["condition_label"],
            "temperature_c": ground["temperature_c"],
            "precipitation_mm": ground["precipitation_mm"],
        },
        "impacts": generate_impacts(ground["condition"], lvl),
        "vulnerable_infrastructure": compute_infra_risk(city_id, ground["condition"], score),
        "population_affected": {
            "estimated": pop_affected_est,
            "evacuation_recommended": score >= 80,
            "shelter_advisory": score >= 55,
        },
        "alerts": generate_alerts(ground, lvl),
    }


@app.get("/api/early-warnings/{city_id}")
def get_early_warnings(city_id: str):
    """AI early warning predictions for a specific city."""
    city = city_by_id(city_id)
    if not city:
        raise HTTPException(status_code=404, detail="Unknown city")
    ground = fetch_ground_truth(city_id)
    return generate_early_warnings(city_id, ground)


# ---------------------------------------------------------------------------
# 7-Day Forecast API (v2.0)
# ---------------------------------------------------------------------------
@app.get("/api/forecast/{city_id}")
def get_forecast(city_id: str):
    """7-day daily forecast + next 24h hourly forecast from Open-Meteo."""
    city = city_by_id(city_id)
    if not city:
        raise HTTPException(status_code=404, detail="Unknown city")

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={city['lat']}&longitude={city['lon']}"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
        "weathercode,wind_speed_10m_max"
        "&hourly=temperature_2m,precipitation,weathercode"
        "&timezone=auto&forecast_days=7"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Forecast API error: {e}")

    # Build daily forecast
    daily = []
    daily_data = data.get("daily", {})
    for i in range(len(daily_data.get("time", []))):
        wc = daily_data["weathercode"][i]
        t_max = daily_data["temperature_2m_max"][i]
        precip_sum = daily_data["precipitation_sum"][i]
        cond = classify_condition(wc, t_max, precip_sum)
        daily.append({
            "date": daily_data["time"][i],
            "temp_max": t_max,
            "temp_min": daily_data["temperature_2m_min"][i],
            "precipitation_sum": precip_sum,
            "wind_speed_max": daily_data["wind_speed_10m_max"][i],
            "weathercode": wc,
            "condition": cond,
            "condition_label": EVENT_LABELS.get(cond, cond),
        })

    # Build next-24h hourly forecast
    hourly = []
    hourly_data = data.get("hourly", {})
    hourly_times = hourly_data.get("time", [])
    now_idx = 0
    try:
        now_str = datetime.now().strftime("%Y-%m-%dT%H:00")
        if now_str in hourly_times:
            now_idx = hourly_times.index(now_str)
    except Exception:
        pass

    for i in range(now_idx, min(now_idx + 24, len(hourly_times))):
        hourly.append({
            "time": hourly_times[i],
            "temperature": hourly_data["temperature_2m"][i],
            "precipitation": hourly_data["precipitation"][i],
            "weathercode": hourly_data["weathercode"][i],
        })

    return {
        "city_id": city_id,
        "city_name": city["name"],
        "daily": daily,
        "hourly": hourly,
    }


# ---------------------------------------------------------------------------
# Analytics / Dashboard API (v2.0)
# ---------------------------------------------------------------------------
@app.get("/api/analytics/summary")
def get_analytics_summary():
    """Dashboard statistics — report counts, verification rates, recent activity."""
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) as c FROM reports").fetchone()["c"]
    verified = conn.execute(
        "SELECT COUNT(*) as c FROM reports WHERE status='verified'"
    ).fetchone()["c"]
    flagged = conn.execute(
        "SELECT COUNT(*) as c FROM reports WHERE status='flagged'"
    ).fetchone()["c"]
    pending = conn.execute(
        "SELECT COUNT(*) as c FROM reports WHERE status='pending'"
    ).fetchone()["c"]
    top_type_row = conn.execute(
        "SELECT type, COUNT(*) as c FROM reports GROUP BY type ORDER BY c DESC LIMIT 1"
    ).fetchone()
    recent = conn.execute(
        "SELECT * FROM reports ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    conn.close()

    return {
        "total_reports": total,
        "verified_count": verified,
        "flagged_count": flagged,
        "pending_count": pending,
        "verified_pct": round(verified / total * 100, 1) if total else 0,
        "top_event_type": dict(top_type_row) if top_type_row else None,
        "recent_reports": [dict(r) for r in recent],
        "last_updated": int(time.time()),
    }


# ---------------------------------------------------------------------------
# Health Check (v2.0)
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health_check():
    """Simple health check endpoint for monitoring."""
    return {
        "status": "ok",
        "timestamp": int(time.time()),
        "version": "2.0.0",
        "cities_tracked": len(CITIES),
    }


# ---------------------------------------------------------------------------
# Startup + static frontend + PWA routes
# ---------------------------------------------------------------------------
init_db()

scheduler = BackgroundScheduler()
scheduler.add_job(ingest_reddit, "interval", minutes=10, next_run_time=datetime.now())
scheduler.add_job(ingest_nasa_eonet, "interval", minutes=15, next_run_time=datetime.now())
scheduler.start()

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")


# PWA: serve service worker and manifest from root scope
@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/manifest.json")
def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/json")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
