# Weather Data Analysis v2.0

A real, working weather verification platform with **impact-based forecasting**.
Live weather data (Open-Meteo), a real SQLite database, an India map
(Leaflet + OpenStreetMap), 7-day forecasts (Chart.js), and genuine
verification logic — reports are checked against live conditions, not mocked.

**Live site:** [weather-data-analysis-steel.vercel.app](https://weather-data-analysis-steel.vercel.app/)

---

## What's New in v2.0

### 🛡️ Impact-Based Forecasting Engine
- **Risk Scoring** — real-time 0-100 risk scores per city based on temperature, precipitation, wind speed, and weather conditions
- **Sector Impact Analysis** — Transport, Agriculture, Health, Power Grid, Emergency Services, Education
- **Infrastructure Vulnerability** — risk assessment for roads, bridges, power substations, hospitals, schools, rail lines
- **AI Early Warnings** — flash flood risk, extreme heat, high wind, visibility, coastal surge alerts
- **Population Exposure** — estimated affected population with evacuation/shelter advisories

### 📊 Analytics Dashboard
- Total reports, verification rates, top event types
- Nationwide risk overview with city risk grid
- Recent activity feed

### 🌡️ 7-Day Forecast
- Daily temperature, precipitation, and wind charts (Chart.js)
- 24-hour hourly temperature forecast
- Condition-based weather icons

### 🎨 UI Overhaul
- **Dashboard** — new default tab with key stats at a glance
- **Dark/Light theme** — toggle with localStorage persistence
- **Auto-refresh** — live data polling every 60 seconds with countdown
- **Loading skeletons** — animated placeholders instead of "Loading…" text
- **Toast notifications** — visual feedback for user actions
- **Report filtering** — filter by status (Verified/Flagged/Pending) and source

### 📱 PWA Support
- Service worker for offline capability
- Web manifest for "Add to Home Screen"
- Cache-first strategy for static assets

### 🔒 Backend Hardening
- Rate limiting on report submission (10/min per IP)
- Health check endpoint (`/api/health`)
- Enhanced weather data: wind speed, humidity, UV index

---

## Part 1 — Run it on your own machine (Ubuntu)

```bash
cd ~/Downloads/weather-data-analysis
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open **http://localhost:8000**. The Dashboard tab loads by default with live weather data.

---

## Part 2 — Host it for real, using GitHub + Render + Vercel

Since you have GitHub and Vercel: the backend (Python + database) needs a
server that stays running, which Vercel's free tier isn't built for — so we
split it. **Render** hosts the backend (free tier, made for exactly this).
**Vercel** hosts the frontend (its actual strength). GitHub connects both.

### Step 1 — Push this project to GitHub

If you don't already have a repo:

1. Go to https://github.com/new, name it `weather-data-analysis`, keep it
   Public, don't add a README (you already have one), click **Create repository**.
2. In your terminal, inside this project folder:
   ```
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR-USERNAME/weather-data-analysis.git
   git push -u origin main
   ```
   Replace `YOUR-USERNAME` with your actual GitHub username. If `git` isn't
   installed: `sudo apt install git -y`.

### Step 2 — Deploy the backend on Render

1. Go to https://render.com and sign up / log in (you can sign in with GitHub directly).
2. Click **New +** → **Web Service**.
3. Connect your GitHub account if asked, then select the `weather-data-analysis` repo.
4. Render should auto-detect the settings from `render.yaml` in this project. If it asks manually, fill in:
   - **Environment:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Plan:** Free
5. Click **Create Web Service**. Wait a few minutes for the first deploy.
6. Once live, Render gives you a URL like:
   ```
   https://weather-data-analysis.onrender.com
   ```
   **Copy this URL** — you need it in the next step.

   Note: on Render's free tier, the backend "sleeps" after 15 minutes of no
   traffic and takes ~30–50 seconds to wake up on the next request. Fine for
   a hackathon demo; mention it if a judge notices the first load is slow.

### Step 3 — Point the frontend at your live backend

Open `static/index.html`, find this line near the top of the `<script>` block:

```js
var API_BASE = 'https://weather-data-analysis-63zg.onrender.com';
```

Change it to your actual Render URL (no trailing slash).

### Step 4 — Deploy the frontend on Vercel

1. Go to https://vercel.com and log in (with GitHub).
2. Click **Add New...** → **Project**.
3. Import the same `weather-data-analysis` GitHub repo.
4. Under **Root Directory**, click **Edit** and set it to `static` — this tells
   Vercel to deploy just the frontend folder, not the Python backend.
5. Framework Preset: choose **Other** (it's a plain static HTML file, no build step needed).
6. Click **Deploy**.
7. Vercel gives you a live URL like `https://weather-data-analysis.vercel.app` —
   that's your public website.

### Step 5 — Test it

Open your Vercel URL. The Dashboard tab should load with live weather data from
your Render backend. Try the Impact Forecast tab to see risk scores, early
warnings, and sector impact analysis. Submit a test report from the Submit tab
to confirm the full loop works end-to-end.

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/cities` | GET | List of tracked cities |
| `/api/ground-truth/{city_id}` | GET | Live weather for a city (temp, precip, wind, humidity, UV) |
| `/api/reports` | GET | All reports |
| `/api/reports` | POST | Submit a new report (with optional photo) |
| `/api/impact-forecast` | GET | Risk scores for all cities |
| `/api/impact-forecast/{city_id}` | GET | Detailed impact assessment for a city |
| `/api/early-warnings/{city_id}` | GET | AI early warnings for a city |
| `/api/forecast/{city_id}` | GET | 7-day daily + 24h hourly forecast |
| `/api/analytics/summary` | GET | Dashboard statistics |
| `/api/source-status` | GET | Data source status |
| `/api/health` | GET | Health check |
| `/api/ingest/reddit` | POST | Trigger Reddit ingestion |
| `/api/ingest/nasa` | POST | Trigger NASA EONET ingestion |

---

## Updating your live site later

Any time you change code:

```
git add .
git commit -m "describe your change"
git push
```

Render and Vercel both auto-redeploy on every push to `main` — no manual
redeploy step needed.

## What to say in your pitch about this setup

It's honest and actually a good talking point: this is a real deployed system
with a live database and live weather data, split across a proper
frontend/backend architecture — closer to how a production system would
actually be built than a single-file demo. The known next steps (social media
ingestion, image dedup) are clearly marked as TODOs in `main.py` for the same
reason — it shows you know the difference between what's built and what's next.

The v2.0 upgrade adds impact-based forecasting — the kind of real-world risk
assessment that disaster management agencies use. The risk scoring is driven
by actual weather parameters, not random numbers.
