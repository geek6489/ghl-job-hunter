# GHL Job Hunter

A personal, always-on job hunting assistant for GoHighLevel, CRM and automation work. Every hour it
searches Google, LinkedIn, X, Facebook, Instagram and remote job boards, removes duplicates, has
Claude score and explain each new opportunity, and emails you the best ones.

See [docs/PLAN.md](docs/PLAN.md) for the architecture, what each platform can and can't reach, and
monthly cost estimates.

## What's in it

- **Hourly monitoring** in a separate worker process, so it runs with your computer off
- **Sources:** Serper/Brave search (Google + `site:` searches for LinkedIn, X, Facebook, Instagram),
  optional SerpApi Google Jobs and X API, and free job-board APIs (Remotive, Remote OK, Himalayas,
  Jobicy, We Work Remotely)
- **Extraction** from official schema.org JobPosting data where sites publish it, then Claude for the
  rest. Missing fields are stored as "Not Specified", never guessed
- **Freshness:** posting date and discovery time kept separately; expired, closed and 404 listings
  dropped; older listings kept while their deadline is in the future
- **Duplicate detection** by canonical URL, platform post id, normalised title + company, and
  description similarity. Cross-platform copies merge into one job with every source link kept
- **Claude analysis** per job: 0-100 score with breakdown, category, summary, matching and missing
  skills, red flags, apply recommendation and a short application strategy
- **Email:** immediate alert for 90+ matches, plus instant / every-3-hours / daily digests, a daily
  Claude summary, and outage alerts. Each job is emailed once
- **Dashboard:** overview KPIs, searchable and filterable job table, job detail, application tracker
  with notes and dates, daily summaries, settings, and a health page with run history, failed
  searches, platform coverage and API spend
- **Security:** keys only in environment variables, password login with a signed session cookie,
  login rate limiting, cross-site POST blocking

Without `ANTHROPIC_API_KEY` the app still runs, but jobs are scored by keyword matching (capped at
89, so nothing is called "Excellent" unless Claude read it) and are labelled "keyword score".

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # fill in at least ADMIN_PASSWORD and SECRET_KEY
set -a; . ./.env; set +a
unset DATABASE_URL          # use SQLite in ./data for local runs
uvicorn app.web.main:app --reload      # dashboard on http://localhost:8000
python -m app.worker                   # in a second terminal: the monitor
pytest -q                              # tests (offline)
```

## Deploy (pick one)

### Free: GitHub Actions + Neon (no card for hosting)

Hourly scans run on GitHub Actions, data lives in a free Neon Postgres database, and the dashboard
runs on Render's free plan or your own computer. Step-by-step: [docs/FREE_SETUP.md](docs/FREE_SETUP.md).

### Render (managed, about $20/month)

1. Push this repository to GitHub.
2. In Render: **New > Blueprint**, choose the repo. It creates the dashboard, the worker and Postgres
   from `render.yaml`.
3. Fill in the secret group `ghl-job-hunter-secrets` (see `.env.example`). Set `PUBLIC_BASE_URL` to
   the web service URL Render gives you.
4. Open the dashboard, sign in, go to **Settings > Send a test email**, then **Health > Run a scan now**.

Render's paid Postgres plans include daily backups.

### Any VPS with Docker (about $5 to $8/month)

```bash
git clone <repo> && cd ghl-job-hunter
cp .env.example .env && nano .env      # set POSTGRES_PASSWORD and your keys
docker compose up -d --build
```

The dashboard listens on port 8000; put Caddy or nginx in front for HTTPS. The `backup` service
writes a gzipped `pg_dump` to `./backups` every day and keeps 14.

### Uptime alerting

The dashboard emails you if the worker stops sending heartbeats. To also be alerted when the whole
server is down, create a free check on healthchecks.io with a 2-hour grace period and put its ping
URL in `HEALTHCHECK_PING_URL`.

## Getting the keys

| Key | Where |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com > API keys |
| `SERPER_API_KEY` | serper.dev (2,500 free queries to start) |
| `BRAVE_API_KEY` | api-dashboard.search.brave.com (alternative to Serper) |
| `SERPAPI_API_KEY` | serpapi.com (optional, structured Google Jobs) |
| `X_BEARER_TOKEN` | developer.x.com, paid plan (optional) |
| `RESEND_API_KEY` | resend.com; verify your sending domain, then use it in `EMAIL_FROM` |

## Layout

```
app/
  worker.py        scheduler: monitor, digests, daily summary, maintenance, heartbeat
  pipeline.py      one run: collect > filter > enrich > dedupe > store > analyse > notify
  sources/         web_search (Serper/Brave), google_jobs (SerpApi), x_api, job_boards
  enrich.py        reads JobPosting JSON-LD from public career pages (robots.txt respected)
  dedupe.py        duplicate detection and consolidation
  analysis.py      Claude structured analysis + labelled keyword fallback
  notify.py        instant alerts and digests;  summary.py  daily summary
  web/             FastAPI dashboard, templates, CSS
tests/             offline tests with recorded-shape API responses
```
