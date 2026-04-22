# Deployment Guide — CCQ Jobs Portal

Step-by-step walkthrough to get from zero to a running production deployment.
Total time: **~30-45 minutes** the first time.

---

## Part 1 — Push to GitHub

```bash
cd ccq-jobs
git init
git add .
git commit -m "Initial commit: CCQ jobs portal MVP"

# Create a new repo on github.com, then:
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/ccq-jobs.git
git push -u origin main
```

---

## Part 2 — Deploy backend + database to Railway

### 2.1 Create the project

1. Go to [railway.app](https://railway.app) and sign in
2. Click **New Project** → **Deploy from GitHub repo**
3. Select your `ccq-jobs` repo
4. Railway auto-detects Python via `nixpacks.toml` and starts building

### 2.2 Add PostgreSQL

1. In your Railway project, click **+ New** → **Database** → **Add PostgreSQL**
2. Railway automatically injects `DATABASE_URL` into your backend service

### 2.3 Set environment variables

On the **backend service** → **Variables** tab, add:

| Key                 | Value                                                |
|---------------------|------------------------------------------------------|
| `ANTHROPIC_API_KEY` | Your Claude API key (from console.anthropic.com)     |
| `ALLOWED_ORIGINS`   | `https://YOUR_USERNAME.github.io`                    |
| `ADMIN_PASSWORD`    | A long random string (you'll need this to access /admin) |
| `CLAUDE_MODEL`      | `claude-sonnet-4-6` (default — can change to haiku for cheaper) |

### 2.4 Initialize the database

Railway provides a Postgres connection string. Run the schema locally or via Railway shell:

**Option A — Local (with Railway CLI):**
```bash
npm install -g @railway/cli
railway login
railway link             # select your project
railway run psql $DATABASE_URL -f db/schema.sql
```

**Option B — psql directly:**
```bash
# Copy DATABASE_URL from Railway's Postgres → Variables tab
psql "postgresql://postgres:PASSWORD@HOST:PORT/railway" -f db/schema.sql
```

### 2.5 Verify backend is up

Railway shows a generated domain like `https://ccq-jobs-production.up.railway.app`.
Visit it — you should see:
```json
{"service":"ccq-jobs-api","status":"ok", "disclaimer":"..."}
```

Also try `/health` and `/docs` (FastAPI auto-generated docs).

---

## Part 3 — Set up the scraper cron

The scraper is a **separate service** on Railway that runs every 2 hours.

1. In your Railway project, click **+ New** → **Empty Service**
2. Name it `scraper`
3. Connect it to the same GitHub repo
4. In **Settings** → **Deploy**:
   - **Start Command**: `cd scraper && python run.py`
5. Copy ALL the same environment variables from the backend service
   (quickest way: use Railway's "Reference variables" feature to point at the backend's vars)
   - Make sure `DATABASE_URL` also points to the shared Postgres plugin
6. In **Settings** → **Cron Schedule**, set: `0 */2 * * *`
   (every 2 hours, on the hour)
7. Remove the healthcheck (scrapers don't have an HTTP endpoint)

### 3.1 Test the scraper once manually

Click **Deploy** → it runs once immediately. Check the logs — you should see:
```
[INFO] scraper.run: Scraping indeed...
[INFO] scraper.indeed: Fetching Indeed page 1: ...
[INFO] scraper.ai_filter: Sending X jobs to Claude...
[INFO] scraper.run: Run N complete.
```

If you see DB records after, it's working. Check via:
```bash
railway run psql $DATABASE_URL -c "SELECT count(*) FROM jobs;"
```

---

## Part 4 — Deploy frontend to GitHub Pages

### 4.1 Update API URL

Edit `frontend/config.js`:

```javascript
window.CCQ_CONFIG = {
    API_BASE: 'https://ccq-jobs-production.up.railway.app',  // ← your Railway URL
    GOOGLE_MAPS_KEY: '',
};
```

Commit and push.

### 4.2 Enable GitHub Pages

1. On your repo → **Settings** → **Pages**
2. **Source**: Deploy from a branch
3. **Branch**: `main` / folder: `/frontend`
4. Click **Save**

After ~1 minute, your site will be live at:
`https://YOUR_USERNAME.github.io/ccq-jobs/`

### 4.3 Update CORS

Go back to Railway backend `ALLOWED_ORIGINS` and set:
```
https://YOUR_USERNAME.github.io
```
(no trailing slash, no path)

Redeploy the backend for the env var to take effect.

---

## Part 5 — Verify end-to-end

1. Open `https://YOUR_USERNAME.github.io/ccq-jobs/`
2. You should see job listings (after the first scraper run has populated the DB)
3. Try filters — region, trade, search
4. Visit `/map.html` — pins should appear for jobs with addresses
5. Visit `/admin.html`, log in with `ADMIN_PASSWORD`, check the review queue

---

## Part 6 — Maintenance

### Check scraper health
- Admin page → **Derniers runs** shows every cron run, successes, failures, stats
- Railway dashboard → scraper service → **Logs** for detailed output

### Claude API costs
Monitor at [console.anthropic.com](https://console.anthropic.com). Expected: **$2-6/month** for hourly-ish runs with ~20-50 new jobs per run.

### Adding a new source
1. Create `scraper/newsource.py` with a `scrape_newsource()` function returning `list[RawJobListing]`
2. Add an `elif` in `scraper/run.py`:
   ```python
   elif source_name == "newsource":
       raw_jobs = scrape_newsource()
   ```
3. Enable the source in DB:
   ```sql
   UPDATE sources SET is_active = TRUE WHERE name = 'newsource';
   ```

### Disabling a source
```sql
UPDATE sources SET is_active = FALSE WHERE name = 'indeed';
```
Next cron run will skip it.

---

## Troubleshooting

### "Impossible de joindre le serveur" on frontend
- Check `config.js` has the right `API_BASE`
- Check Railway backend is actually running (visit the root URL directly)
- Check CORS — `ALLOWED_ORIGINS` must match your frontend URL exactly

### "401 Unauthorized" on admin
- `ADMIN_PASSWORD` env var not set, or the password you entered doesn't match

### Scraper gets 0 jobs
- Indeed may have changed their HTML selectors — check `scraper/indeed.py` selectors against current live HTML
- Indeed may have blocked your Railway IP — rare but possible. Fallback options:
  1. Switch to [SerpAPI](https://serpapi.com/indeed-search-api) or similar for ~$50/mo
  2. Run the scraper on a different infra (e.g. GitHub Actions, which uses rotating IPs)

### Claude returns invalid JSON
- Check `scraper/ai_filter.py` logs — Claude sometimes wraps in ```json despite the system prompt. The `_extract_json` helper strips that.
- If systematic, tighten the system prompt or switch to `claude-haiku-4-5` for faster, cheaper runs.

---

## Going to V2 — accounts & notifications

When you're ready to add user accounts:
1. Add Supabase or Clerk for auth (both free tiers are plenty)
2. Add `users` + `user_preferences` + `saved_jobs` tables
3. Add `/api/users/me/preferences` endpoints
4. Add an email provider (Resend is cheapest, $20/mo for 50k emails)
5. Add a second cron: `scraper/send_notifications.py` that checks new jobs against user prefs

That's another few days of work — but the foundation is ready for it.
