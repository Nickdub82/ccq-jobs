# CCQ Jobs Portal — Montreal Painter Edition

A job aggregation portal for Quebec construction workers, focused on CCQ-compliant painter jobs in Montreal. Scrapes public job boards every 2 hours, filters with AI, displays on a clean web interface.

## What this does

- **Scrapes Indeed** every 2 hours for painter/CCQ jobs in Montreal
- **Claude AI processes** the scraped data: confirms CCQ relevance, structures messy fields, flags uncertain listings
- **PostgreSQL stores** clean job data
- **FastAPI backend** exposes job data via REST API
- **Plain HTML/JS frontend** displays listings with filters and Google Maps view
- **Review queue** for jobs Claude isn't 100% sure about

## Architecture

```
┌─────────────────┐      ┌──────────────────┐      ┌─────────────────┐
│  Indeed scraper │─────▶│  Claude API      │─────▶│   PostgreSQL    │
│  (every 2h)     │      │  (filter + clean)│      │   (Railway)     │
└─────────────────┘      └──────────────────┘      └─────────────────┘
                                                            │
                                                            ▼
                          ┌─────────────────┐      ┌─────────────────┐
                          │  GitHub Pages   │◀─────│  FastAPI backend│
                          │  (HTML/JS)      │      │  (Railway)      │
                          └─────────────────┘      └─────────────────┘
```

## Stack

| Layer          | Choice                      | Why                              |
|----------------|-----------------------------|----------------------------------|
| Frontend       | Plain HTML + JS             | Simple, hosts on GitHub Pages    |
| Backend        | FastAPI (Python)            | Fast, modern, auto API docs      |
| Database       | PostgreSQL                  | You already know it              |
| Scraper        | Python + Playwright/httpx   | Robust against anti-bot          |
| AI             | Claude API (Sonnet)         | Filtering + structuring          |
| Hosting (back) | Railway                     | You already use it               |
| Hosting (front)| GitHub Pages                | Free, push-to-deploy             |
| Scheduler      | Railway cron                | Built-in, zero setup             |

## Project structure

```
ccq-jobs/
├── backend/          # FastAPI app
│   ├── main.py       # API entry point
│   ├── db.py         # Database connection
│   ├── models.py     # SQLAlchemy models
│   ├── schemas.py    # Pydantic schemas
│   ├── routes/       # API endpoints
│   └── requirements.txt
├── scraper/          # Scraping + Claude pipeline
│   ├── run.py        # Main scraper entry (run by cron)
│   ├── indeed.py     # Indeed scraper module
│   ├── ai_filter.py  # Claude API processing
│   ├── dedup.py      # Deduplication logic
│   └── requirements.txt
├── frontend/         # Static HTML/JS
│   ├── index.html    # Job listings page
│   ├── map.html      # Map view page
│   ├── admin.html    # Review queue page
│   ├── app.js        # Main JS logic
│   ├── map.js        # Map logic
│   └── style.css
├── db/
│   └── schema.sql    # Initial PostgreSQL schema
├── railway.toml      # Railway deployment config
├── Procfile          # Process definitions
└── README.md
```

## Quick start (local)

```bash
# 1. Clone and enter
git clone <your-repo>
cd ccq-jobs

# 2. Setup Python env
python -m venv venv
source venv/bin/activate  # or .\venv\Scripts\activate on Windows
pip install -r backend/requirements.txt
pip install -r scraper/requirements.txt

# 3. Set environment variables (.env)
cp .env.example .env
# Fill in DATABASE_URL, ANTHROPIC_API_KEY, etc.

# 4. Initialize DB
psql $DATABASE_URL -f db/schema.sql

# 5. Run backend
cd backend && uvicorn main:app --reload

# 6. Run scraper once (manual)
cd scraper && python run.py

# 7. Serve frontend
cd frontend && python -m http.server 8080
```

## Deployment

1. Push this repo to GitHub
2. On Railway: New Project → Deploy from GitHub → point at this repo
3. Add environment variables (see `.env.example`)
4. Railway auto-detects `railway.toml` and deploys backend + scraper cron
5. For frontend: GitHub repo → Settings → Pages → deploy from `/frontend`

## Environment variables

See `.env.example` for the full list. Minimum required:

- `DATABASE_URL` — PostgreSQL connection string
- `ANTHROPIC_API_KEY` — Claude API key
- `ALLOWED_ORIGINS` — comma-separated list for CORS (your GitHub Pages URL)

## Cost estimate (monthly)

- Railway Postgres + backend: ~$5-10
- Claude API (Sonnet, ~12 runs/day): ~$3-8
- GitHub Pages: free
- **Total: ~$10-20/month**

## Legal disclaimer

This portal aggregates **publicly available** job postings only. It is **not** affiliated with the CCQ. All listings link back to the original source. Users must apply through the original channels. This tool does not perform placement, referencing, or any CCQ-regulated activity.

## Roadmap

- **V1 (now):** Indeed scraper, Montreal only, list + map view, admin review queue, no accounts
- **V2:** Add Jobboom + Jobillico, other regions, user accounts, email notifications
- **V3:** Facebook groups scraper, SMS/push notifications, advanced filters, analytics
