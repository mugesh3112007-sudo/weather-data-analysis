# Weather Data Analysis

A real, working weather verification platform. Live weather data (Open-Meteo),
a real SQLite database, an India map (Leaflet + OpenStreetMap), and genuine
verification logic — reports are checked against live conditions, not mocked.

---

## Part 1 — Run it on your own machine (Ubuntu)

```
cd ~/Downloads/weather-data-analysis
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open **http://localhost:8000**. See the earlier setup guide in this conversation
for troubleshooting each step (missing `python3-venv`, wrong folder, etc.) —
the commands are identical here.

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
var API_BASE = '';
```

Change it to your actual Render URL (no trailing slash):

```js
var API_BASE = 'https://weather-data-analysis.onrender.com';
```

Save, then push the change:

```
git add static/index.html
git commit -m "Point frontend at Render backend"
git push
```

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

Open your Vercel URL. The map should load, pulling live data from your Render
backend. Submit a test report from the Submit tab to confirm the full loop
(frontend → Render backend → Open-Meteo → SQLite → back to frontend) works
end-to-end.

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
