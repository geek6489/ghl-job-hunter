# Free setup (no card for hosting)

```
GitHub Actions (hourly)  ──writes──►  Neon Postgres (free)  ◄──reads──  Dashboard (Render free, or your laptop)
```

- **Monitoring** runs on GitHub Actions every hour at :07 UTC. It doesn't depend on the dashboard
  being up or your computer being on.
- **Database** is Neon's free Postgres plan.
- **Dashboard** runs on Render's free web plan. It sleeps after about 15 minutes without visitors
  and takes about a minute to wake on the next visit. That only affects the dashboard: the hourly
  scans and the emails don't need it.

Hosting costs nothing. The Anthropic API and Serper still need credit (Serper's first 2,500 searches
are free). Resend's free plan covers the emails.

## 1. Create the database (Neon)

1. Sign up at neon.tech and create a project (any region).
2. Copy the connection string. It looks like
   `postgresql://user:password@ep-xxx.region.aws.neon.tech/neondb?sslmode=require`.
   This is your `DATABASE_URL`.

## 2. Add the secrets to GitHub

In the repo on GitHub: **Settings > Secrets and variables > Actions**.

**Secrets** tab (stay hidden, even in a public repo):

| Name | Value |
|---|---|
| `DATABASE_URL` | the Neon connection string |
| `ANTHROPIC_API_KEY` | from console.anthropic.com |
| `SERPER_API_KEY` | from serper.dev |
| `RESEND_API_KEY` | from resend.com |
| `HEALTHCHECK_PING_URL` | optional, from healthchecks.io |
| `SERPAPI_API_KEY`, `X_BEARER_TOKEN`, `BRAVE_API_KEY` | optional |

**Variables** tab (not secret):

| Name | Value |
|---|---|
| `EMAIL_FROM` | e.g. `GHL Job Hunter <alerts@yourdomain.com>` (a domain verified in Resend; for testing, Resend allows `onboarding@resend.dev` sending to your own address) |
| `EMAIL_TO` | your email |
| `PUBLIC_BASE_URL` | your dashboard URL from step 4 (used for links in emails) |
| `APP_TIMEZONE` | `Asia/Karachi` (default) |
| `CLAUDE_MODEL` | optional; `claude-haiku-5-5` is much cheaper (also set `CLAUDE_INPUT_PRICE_PER_MTOK=0.10` and `CLAUDE_OUTPUT_PRICE_PER_MTOK=0.50`) |

## 3. Start the monitor

**Actions** tab > **Hourly job monitor** > **Run workflow**. Open the run to see its log. After
that it runs every hour by itself.

## 4. Dashboard on Render (free)

1. render.com > **New > Blueprint**, pick this repo, and set the Blueprint file to `render-free.yaml`.
2. Fill in `DATABASE_URL` (same Neon string), `ADMIN_PASSWORD`, `PUBLIC_BASE_URL` (the
   `https://...onrender.com` address Render shows), and the email/Anthropic values (used for test
   emails, "Re-run AI analysis" and outage alerts).
3. Open the URL and sign in with `admin` and your password.

### Or: dashboard on Vercel (free, no card)

If Render asks for a card, use Vercel's free Hobby plan instead.

1. Sign up at vercel.com with GitHub, then **Add New > Project** and import this repo.
2. Leave the framework preset as detected (FastAPI or Other). Under **Environment Variables** add
   `DATABASE_URL`, `ADMIN_PASSWORD`, `SECRET_KEY` (any 40+ random characters), `APP_TIMEZONE`
   (`Asia/Karachi`) and `PUBLIC_BASE_URL` (your final https address). Click **Deploy**.
3. Custom domain: project **Settings > Domains**, add e.g. `jobs.yourdomain.com`, then add the CNAME
   record Vercel shows at your DNS provider.

On Vercel the dashboard runs only while someone is using it, so its "monitoring stopped" watchdog
is off there. GitHub still emails you when an hourly run fails.

If Render asks for a card for this too, skip it. Run the dashboard on your own computer only when
you want to look, pointed at the same database:

```bash
pip install -r requirements.txt
DATABASE_URL='<neon string>' ADMIN_PASSWORD='<pick one>' SECRET_KEY='<any 40 characters>' \
  uvicorn app.web.main:app
# open http://localhost:8000
```

## Things to know

- **"Run a scan now"** in the dashboard queues a scan for the next hourly run. For an immediate
  scan, use **Run workflow** on GitHub.
- **Failures:** if 3 scans in a row fail, you get an email from the app, and GitHub also emails you
  about failed workflow runs. For an alert even when GitHub itself skips runs, add a healthchecks.io
  check (period 1 hour, grace 2 hours) and set `HEALTHCHECK_PING_URL`.
- **This repository is public.** The code is visible, but secrets and the job data aren't (the jobs
  are stored in Neon). Public repos get unlimited Actions minutes. If you make the repo private,
  the free allowance is 2,000 minutes a month, and each run uses about 1 to 2.
- **60-day rule:** GitHub pauses scheduled workflows in public repos after 60 days with no commits.
  GitHub emails you first; click **Enable workflow** in the Actions tab, or push any commit.
- The Settings page's scan interval still applies: with "every 3 hours" the hourly run skips scans
  until 3 hours have passed. Intervals shorter than an hour aren't possible on this setup.
