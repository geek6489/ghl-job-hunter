# GHL Job Hunter: architecture, platform access and costs

This is the pre-development plan from the spec (section 12), updated to match what was built.

## 1. Architecture

```
            ┌──────────────────────── worker (python -m app.worker, runs 24/7) ────────────────────────┐
            │  APScheduler 1-minute dispatcher reads the schedule from the DB                           │
            │                                                                                           │
            │  monitor run (hourly by default)                                                          │
            │   1. sources ── Serper/Brave site: searches ─ Google, LinkedIn, X, Facebook, Instagram     │
            │              ── SerpApi Google Jobs (optional)                                            │
            │              ── X API v2 recent search (optional, paid)                                   │
            │              ── Remotive, Remote OK, Himalayas, Jobicy, We Work Remotely (free)           │
            │   2. relevance pre-filter (GHL / CRM / automation terms)                                  │
            │   3. enrichment: read schema.org JobPosting on public career pages (robots.txt respected) │
            │   4. expiry checks (validThrough, "no longer accepting", 404/410, max age)                │
            │   5. dedupe: canonical URL → platform post id → title+company → description similarity    │
            │   6. Claude analysis (structured output): fields, 0-100 score, skills, red flags, advice  │
            │   7. instant email for 90+ (and per-scan digest in "instant" mode)                        │
            │  digest tick (3h / daily) · daily Claude summary · daily maintenance · heartbeat          │
            └─────────────────────────────────────────────┬─────────────────────────────────────────────┘
                                                          │
                                                   PostgreSQL (jobs, sources, keywords, settings,
                                                   runs, search logs, API usage, emails, summaries)
                                                          │
            ┌─────────────────────────── web (uvicorn app.web.main:app) ───────────────────────────────┐
            │  Server-rendered dashboard: overview, jobs + filters, job detail, tracker, summaries,     │
            │  settings, health. Password login, signed session cookie, cross-site POST blocking.       │
            │  Watchdog: emails you if worker heartbeats stop.                                           │
            └───────────────────────────────────────────────────────────────────────────────────────────┘
```

Stack choices, and where they differ from the spec's suggestions:

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.12, FastAPI | Spec's suggestion; one language for worker and web |
| Frontend | Server-rendered Jinja + CSS (no Next.js) | One deployable service instead of two, no API layer to secure, fast on mobile. Swappable later because all data is in Postgres and `/api/jobs` exists |
| Database | PostgreSQL (SQLite for local dev) | Spec |
| Background jobs | Separate worker process with APScheduler | Celery + Redis adds a broker and a second always-on service for a single-user workload that runs once an hour. The worker is independent of the web app, which is what the spec asks for |
| AI | Claude API, structured outputs, `claude-opus-5-5` by default | Model is one env var (`CLAUDE_MODEL`) |
| Email | Resend API or any SMTP server | Spec |
| Search | Serper.dev (Google results) or Brave Search API; SerpApi Google Jobs optional | Google's own Custom Search JSON API is closed to new customers and is being retired, so it isn't used |
| Hosting | Render Blueprint (`render.yaml`) or Docker Compose on any VPS | Both keep the worker running when your computer is off |

## 2 & 3. Platform access and restrictions

| Platform | Status | How it's searched | What it can't see |
|---|---|---|---|
| Google | **Fully integrated** (with a search key) | Serper web search for career pages and job boards (last 24h, weekly sweep once a day), plus SerpApi Google Jobs if you add that key. Job pages are opened to read their official JobPosting data | Only what Google has indexed |
| Job boards | **Fully integrated** (no key) | Remotive, Remote OK, Himalayas, Jobicy, We Work Remotely public APIs / RSS | Remote roles only; polled every 2 to 6 hours as these boards ask |
| LinkedIn | **Partial** | Public LinkedIn job pages and posts that Google has indexed (`site:linkedin.com/jobs/view`, `site:linkedin.com/posts`), and LinkedIn listings that Google Jobs aggregates | LinkedIn has no public job-search API, and its terms forbid scraping. Logged-in-only content, most recruiter posts and anything not indexed are out of reach. LinkedIn's Job Posting API is only for ATS partners |
| X | **Partial**, or **Full with a paid X API key** | Indexed public posts via `site:x.com`; with `X_BEARER_TOKEN`, the official v2 recent-search endpoint | Search-engine coverage of X is thin and delayed. The X API is paid; check developer.x.com for current pricing |
| Facebook | **Partial** | Public pages and public group posts that Google has indexed | Meta shut down the Groups API in April 2024 and has no public post search. Private groups, where most freelance GHL posts live, cannot be monitored by any permitted method |
| Instagram | **Partial** | Public posts that Google has indexed | Hashtag search through the Graph API needs an Instagram Business account, a Meta app and app review, and is limited to 30 hashtags per week. Not built; can be added if you want to go through Meta's review |

The app never logs in to these sites and never scrapes them directly. Social pages are not fetched at all; only their search-result title and snippet are used, so social listings are often thin and Claude marks unknown fields "Not Specified". The Health page shows this table live, based on which keys are configured.

## 4. Monthly cost estimate (USD)

Assumes the defaults: hourly scans, 18 search queries per scan (the 20 keywords are grouped with OR and rotated, so every keyword × platform combination is covered roughly every 2 hours), 20 to 60 new relevant jobs a day reaching Claude.

| Item | Estimate | Notes |
|---|---|---|
| Hosting, Render | about $20 | Web starter $7 + worker starter $7 + Postgres basic about $6 |
| Hosting, VPS alternative | about $5 to $8 | e.g. a small Hetzner or DigitalOcean box with `docker compose up -d` |
| Serper.dev | about $13 | ~13,000 queries/month at roughly $1 per 1,000 (prepaid credits; 2,500 free on signup) |
| Brave Search API (instead of Serper) | about $65 | ~$5 per 1,000 queries |
| SerpApi Google Jobs (optional) | $25 to $75 | ~1,440 searches/month; depends on plan |
| Claude, Opus 5.5 ($4 / $20 per million tokens) | about $20 to $65 | ~$0.035 per job analysed at low effort, plus about $1 for daily summaries |
| Claude, Haiku 5.5 instead | about $1 to $3 | set `CLAUDE_MODEL=claude-haiku-5-5` and update the two price variables |
| Resend email | $0 | free tier: 3,000 emails/month |
| X API (optional) | varies | paid plan required |

Typical total: **about $55 to $100/month** on Render with Opus and Serper, or **about $20 to $30** on a VPS with Haiku. These are estimates; the Health page tracks actual API spend from every call and projects the month.

## 5. What works immediately vs. what needs your accounts

Works as soon as it's deployed, no external accounts: job-board monitoring, database, dedupe, keyword scoring, dashboard, tracker, settings, health page, daily stats.

Needs your keys (you create these accounts; the app reads them from environment variables):

| Feature | Needs |
|---|---|
| AI scoring, summaries, red flags, strategy, daily narrative | `ANTHROPIC_API_KEY` |
| Google, LinkedIn, X, Facebook, Instagram coverage | `SERPER_API_KEY` (or `BRAVE_API_KEY`) |
| Structured Google Jobs listings | `SERPAPI_API_KEY` (optional) |
| Direct X search | `X_BEARER_TOKEN` (optional, paid) |
| Email alerts, digests, daily summary, outage alerts | `RESEND_API_KEY` + a verified sender domain, or SMTP details |
| Alert when the whole server is down | `HEALTHCHECK_PING_URL` (free healthchecks.io check) |
| 24/7 operation | A host: Render account or a VPS |

Not possible within platform terms: full LinkedIn recruiter-post monitoring, private Facebook groups, logged-in Instagram content.
