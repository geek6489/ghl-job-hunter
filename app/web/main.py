"""Dashboard (FastAPI + server-rendered Jinja templates). Run: uvicorn app.web.main:app"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from starlette.middleware.sessions import SessionMiddleware

from .. import settings_store
from ..analysis import category_for
from ..board import LANES, application_message, lane_for

APPLIED_STATUSES = ["Applied", "Interview Scheduled", "Offer Received", "Rejected"]
from ..config import get_config
from ..coverage import platform_coverage
from ..db import init_db, session_scope
from ..models import (
    JOB_STATUSES, NOT_SPECIFIED, PLATFORMS, TRACKER_STATUSES, ApiUsage, DailySummary, Job, Keyword, MonitorRun,
    Notification, SearchLog, utcnow,
)
from ..notify import fmt_local, send_system_alert

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["local"] = fmt_local
templates.env.globals["NOT_SPECIFIED"] = NOT_SPECIFIED


def heartbeat_limit(s) -> timedelta:
    """How old the worker heartbeat may be before we call it stopped. The always-on worker beats
    every minute; the GitHub Actions schedule only beats once per hourly run."""
    if settings_store.get(s, "worker_mode") == "scheduled":
        return timedelta(minutes=max(int(settings_store.get(s, "monitor_interval_minutes", 60)), 60) + 45)
    return timedelta(minutes=15)


def score_class(score):
    if score is None:
        return "s-none"
    return "s-ex" if score >= 90 else "s-strong" if score >= 75 else "s-pot" if score >= 60 else "s-low"


templates.env.filters["score_class"] = score_class


def ago(dt) -> str:
    if not dt:
        return ""
    secs = max(0, int((utcnow() - dt).total_seconds()))
    if secs < 3600:
        return f"{max(1, secs // 60)}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


templates.env.filters["ago"] = ago


def today_local() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(get_config().timezone)).strftime("%A, %d %B %Y")


templates.env.globals["today_local"] = today_local
templates.env.globals["lane_of"] = lane_for


async def watchdog_loop():
    """If the worker stops sending heartbeats, email the owner (once per outage)."""
    while True:
        await asyncio.sleep(600)
        try:
            with session_scope() as s:
                hb = settings_store.get(s, "worker_heartbeat")
                interval = int(settings_store.get(s, "monitor_interval_minutes", 60))
                last_run = settings_store.get(s, "last_run_finished_at")
                alerted = settings_store.get(s, "watchdog_alerted_at")
                now = utcnow()
                stale_hb = not hb or now - datetime.fromisoformat(hb) > heartbeat_limit(s)
                stale_run = last_run and now - datetime.fromisoformat(last_run) > timedelta(minutes=interval * 2 + 30)
                if (stale_hb or stale_run) and not alerted:
                    body = (f"Last worker heartbeat: {fmt_local(datetime.fromisoformat(hb)) if hb else 'never'}\n"
                            f"Last finished run: {fmt_local(datetime.fromisoformat(last_run)) if last_run else 'never'}\n"
                            "The hourly monitor appears to have stopped. Check the worker service logs.")
                    if send_system_alert(s, "GHL Job Hunter: hourly monitoring has stopped", body):
                        settings_store.put(s, "watchdog_alerted_at", now.isoformat())
                elif not (stale_hb or stale_run) and alerted:
                    settings_store.put(s, "watchdog_alerted_at", None)
        except Exception:
            log.exception("watchdog check failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with session_scope() as s:
        settings_store.ensure_defaults(s)
    # On serverless hosts (Vercel) nothing runs between requests, so the watchdog can't work there.
    # The hourly GitHub Actions run still emails on failures.
    task = None if os.environ.get("VERCEL") else asyncio.create_task(watchdog_loop())
    yield
    if task:
        task.cancel()


cfg = get_config()
app = FastAPI(title="GHL Job Hunter", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

PUBLIC_PATHS = {"/login", "/healthz"}
_login_attempts: dict[str, list[float]] = defaultdict(list)


@app.middleware("http")
async def auth_and_csrf(request: Request, call_next):
    path = request.url.path
    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlsplit(origin).netloc != request.url.netloc:
            return JSONResponse({"detail": "cross-site request blocked"}, status_code=403)
    if path not in PUBLIC_PATHS and not path.startswith("/static") and not request.session.get("user"):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "not authenticated"}, status_code=401)
        return RedirectResponse("/login?" + urlencode({"next": path}), status_code=303)
    return await call_next(request)


# SessionMiddleware must wrap the auth middleware, so it is added after it.
if not cfg.secret_key or len(cfg.secret_key) < 32:
    log.warning("SECRET_KEY missing or short; sessions will not survive restarts")
app.add_middleware(
    SessionMiddleware, secret_key=cfg.secret_key or __import__("secrets").token_urlsafe(48),
    same_site="strict", https_only=cfg.public_base_url.startswith("https"), max_age=14 * 24 * 3600,
)


def render(request: Request, name: str, **ctx):
    ctx.setdefault("nav", name.split(".")[0])
    return templates.TemplateResponse(request, name, ctx)


# --------------------------------------------------------------------------- auth
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", next=next, error=None, configured=bool(cfg.admin_password))


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    ip = request.client.host if request.client else "?"
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < 900]
    if len(_login_attempts[ip]) >= 8:
        return render(request, "login.html", next=next, error="Too many attempts. Try again in 15 minutes.", configured=True)
    ok = bool(cfg.admin_password) and hmac.compare_digest(username, cfg.admin_username) and hmac.compare_digest(
        password.encode(), cfg.admin_password.encode())
    if not ok:
        _login_attempts[ip].append(now)
        return render(request, "login.html", next=next, error="Wrong username or password.", configured=bool(cfg.admin_password))
    request.session.clear()
    request.session["user"] = username
    return RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
def healthz():
    with session_scope() as s:
        hb = settings_store.get(s, "worker_heartbeat")
        fresh = bool(hb) and utcnow() - datetime.fromisoformat(hb) < heartbeat_limit(s)
    return {"web": "ok", "worker": "ok" if fresh else "stale", "worker_heartbeat": hb}


# --------------------------------------------------------------------------- overview
@app.get("/", response_class=HTMLResponse)
def overview(request: Request, days: int = 30):
    days = days if days in (1, 3, 7, 30) else 30
    with session_scope() as s:
        prefs = settings_store.get_all(s)
        base = select(func.count(Job.id)).where(Job.is_job_posting.is_(True))
        since = utcnow() - timedelta(hours=24)
        stats = {
            "total": s.scalar(base),
            "today": s.scalar(base.where(Job.discovered_at >= since)),
            "strong": s.scalar(base.where(Job.score >= 75, Job.status.in_(["New", "Active"]))),
            "applied": s.scalar(base.where(Job.tracker_status.in_(APPLIED_STATUSES))),
        }
        cutoff = utcnow() - timedelta(days=days)
        jobs = list(s.scalars(
            select(Job).where(Job.is_job_posting.is_(True), Job.status.in_(["New", "Active"]), Job.discovered_at >= cutoff,
                              Job.tracker_status.not_in(APPLIED_STATUSES + ["Not Interested"]))
            .order_by(Job.discovered_at.desc()).limit(600)
        ))
        lanes = {key: [] for key, _, _ in LANES}
        for j in jobs:
            _ = j.sources
            lanes[lane_for(j.title)].append({"job": j, "msg": application_message(j, prefs)})
        applied_jobs = list(s.scalars(
            select(Job).where(Job.tracker_status.in_(APPLIED_STATUSES))
            .order_by(Job.applied_at.desc().nulls_last(), Job.tracker_updated_at.desc().nulls_last()).limit(600)
        ))
        applied = {key: [] for key, _, _ in LANES}
        for j in applied_jobs:
            _ = j.sources
            applied[lane_for(j.title)].append({"job": j, "msg": application_message(j, prefs)})
        week_ago = utcnow() - timedelta(days=7)
        stats["applied_week"] = sum(1 for j in applied_jobs if j.applied_at and j.applied_at >= week_ago)
        last_run = s.scalar(select(MonitorRun).order_by(MonitorRun.id.desc()).limit(1))
        # Hourly scan log: the latest runs, newest first, with a note on gaps longer than an hour and a half
        from zoneinfo import ZoneInfo
        _tz = ZoneInfo(cfg.timezone)
        scan_log, prev = [], None
        for r in s.scalars(select(MonitorRun).order_by(MonitorRun.id.desc()).limit(12)):
            st = r.stats or {}
            gap_h = round((prev - r.started_at).total_seconds() / 3600) if prev else 0
            scan_log.append({"at": r.started_at.replace(tzinfo=timezone.utc).astimezone(_tz), "status": r.status,
                             "trigger": r.trigger, "found": st.get("raw", 0), "new": st.get("new", 0),
                             "emailed": st.get("emailed", 0), "gap_before": gap_h if gap_h >= 2 else 0,
                             "error": (r.error or "")[-300:]})
            prev = r.started_at
        since_last_h = (utcnow() - last_run.started_at).total_seconds() / 3600 if last_run else None
        nxt = datetime.now(_tz).replace(second=0, microsecond=0)
        nxt = nxt.replace(minute=7) + (timedelta(hours=1) if nxt.minute >= 7 else timedelta(0))
        scan_info = {"next": nxt, "today": datetime.now(_tz).date(), "overdue": since_last_h is not None and since_last_h > 1.6,
                     "hours_since": int(since_last_h) if since_last_h else 0}
        # Jobs found per day for the last 7 days (local time), for the small chart
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(cfg.timezone)
        today = datetime.now(tz).date()
        found = s.scalars(select(Job.discovered_at).where(
            Job.is_job_posting.is_(True), Job.discovered_at >= utcnow() - timedelta(days=8)))
        per_day = defaultdict(int)
        for d in found:
            per_day[d.replace(tzinfo=timezone.utc).astimezone(tz).date()] += 1
        week = [per_day[today - timedelta(days=i)] for i in range(6, -1, -1)]
        active = s.scalar(base.where(Job.status.in_(["New", "Active"]))) or 0
        stats["strong_pct"] = round(100 * stats["strong"] / active) if active else 0
        stats["lanes"] = {k: len(v) for k, v in lanes.items()}
        hour = datetime.now(tz).hour
        greeting = "Good morning" if 5 <= hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
        first_name = (prefs.get("applicant_name") or "").split(" ")[0]
        return render(request, "overview.html", stats=stats, lanes=lanes, lane_info=LANES, days=days, week=week,
                      greeting=greeting, first_name=first_name, applied=applied,
                      last_run=last_run, claude=cfg.claude_enabled, now=utcnow(),
                      scan_log=scan_log, scan_info=scan_info)


# --------------------------------------------------------------------------- jobs
SORTS = {
    "score": (Job.score.desc().nulls_last(), Job.discovered_at.desc()),
    "newest": (Job.discovered_at.desc(),),
    "posted": (Job.posted_at.desc().nulls_last(),),
    "salary": (Job.salary_usd_annual.desc().nulls_last(),),
}


def _job_query(f: dict):
    q = select(Job)
    if not f.get("show_non_jobs"):
        q = q.where(Job.is_job_posting.is_(True))
    if f.get("q"):
        like = f"%{f['q']}%"
        q = q.where(or_(Job.title.ilike(like), Job.company.ilike(like), Job.description.ilike(like)))
    if f.get("title"):
        q = q.where(Job.title.ilike(f"%{f['title']}%"))
    if f.get("platform"):
        q = q.where(Job.primary_platform == f["platform"])
    if f.get("min_score"):
        q = q.where(Job.score >= int(f["min_score"]))
    if f.get("max_score"):
        q = q.where(Job.score <= int(f["max_score"]))
    if f.get("country"):
        like = f"%{f['country']}%"
        q = q.where(or_(Job.country.ilike(like), Job.location.ilike(like)))
    if f.get("arrangement"):
        q = q.where(Job.work_arrangement == f["arrangement"])
    if f.get("employment"):
        q = q.where(Job.employment_type == f["employment"])
    if f.get("salary_min"):
        q = q.where(Job.salary_usd_annual >= float(f["salary_min"]))
    if f.get("salary_max"):
        q = q.where(Job.salary_usd_annual <= float(f["salary_max"]))
    if f.get("posted_within"):
        cutoff = utcnow() - timedelta(days=int(f["posted_within"]))
        q = q.where(or_(Job.posted_at >= cutoff, (Job.posted_at.is_(None)) & (Job.discovered_at >= cutoff)))
    if f.get("status"):
        q = q.where(Job.status == f["status"])
    if f.get("tracker"):
        q = q.where(Job.tracker_status == f["tracker"])
    return q


@app.get("/jobs", response_class=HTMLResponse)
def jobs_list(request: Request, page: int = 1):
    f = {k: v for k, v in request.query_params.items() if v and k != "page"}
    sort = f.get("sort", "score")
    per = 50
    with session_scope() as s:
        q = _job_query(f)
        total = s.scalar(select(func.count()).select_from(q.subquery()))
        jobs = list(s.scalars(q.order_by(*SORTS.get(sort, SORTS["score"])).offset((page - 1) * per).limit(per)))
        for j in jobs:
            _ = j.sources
        qs = urlencode({k: v for k, v in f.items()})
        return render(request, "jobs.html", jobs=jobs, f=f, total=total, page=page, pages=max(1, -(-total // per)),
                      qs=qs, platforms=PLATFORMS, statuses=JOB_STATUSES, trackers=TRACKER_STATUSES)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str):
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job:
            raise HTTPException(404)
        _ = job.sources
        msg = application_message(job, settings_store.get_all(s))
        return render(request, "job_detail.html", job=job, trackers=TRACKER_STATUSES, statuses=JOB_STATUSES,
                      a=job.analysis or {}, nav="jobs", msg=msg)


@app.post("/jobs/{job_id}/tracker")
def update_tracker(job_id: str, tracker_status: str = Form(...), notes: str = Form(""), applied_at: str = Form(""),
                   status: str = Form("")):
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job:
            raise HTTPException(404)
        if tracker_status in TRACKER_STATUSES:
            job.tracker_status = tracker_status
        if status in JOB_STATUSES:
            job.status = status
        job.notes = notes[:20000]
        if applied_at:
            job.applied_at = datetime.fromisoformat(applied_at)
        elif tracker_status == "Applied" and not job.applied_at:
            job.applied_at = utcnow()
        job.tracker_updated_at = utcnow()
    return RedirectResponse(f"/jobs/{job_id}?saved=1", status_code=303)


@app.post("/jobs/{job_id}/applied")
def mark_applied(job_id: str, applied: str = Form("1")):
    """Quick toggle used by the board's Applied button. Returns JSON so the page can move the card without reloading."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job:
            raise HTTPException(404)
        if applied == "1":
            job.tracker_status = "Applied"
            job.applied_at = job.applied_at or utcnow()
        else:
            job.tracker_status = "New"
            job.applied_at = None
        job.tracker_updated_at = utcnow()
        return {"id": job.id, "tracker_status": job.tracker_status,
                "applied_at": job.applied_at.isoformat() if job.applied_at else None}


@app.post("/jobs/{job_id}/reanalyze")
def reanalyze(job_id: str):
    from ..pipeline import analyze_jobs

    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job:
            raise HTTPException(404)
        stats: dict = {}
        analyze_jobs(s, [job], stats)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/tracker", response_class=HTMLResponse)
def tracker(request: Request):
    with session_scope() as s:
        jobs = list(s.scalars(select(Job).where(Job.tracker_status != "New").order_by(Job.tracker_updated_at.desc().nulls_last())))
        columns = {st: [j for j in jobs if j.tracker_status == st] for st in TRACKER_STATUSES if st != "New"}
        return render(request, "tracker.html", columns=columns)


# --------------------------------------------------------------------------- summaries
@app.get("/summaries", response_class=HTMLResponse)
def summaries(request: Request):
    with session_scope() as s:
        items = list(s.scalars(select(DailySummary).order_by(DailySummary.day.desc()).limit(60)))
        return render(request, "summaries.html", items=items)


@app.post("/summaries/generate")
def summaries_generate(send: str = Form("")):
    from ..summary import email_summary, generate_daily_summary

    with session_scope() as s:
        summary = generate_daily_summary(s, force=True)
        if summary and send:
            email_summary(s, summary)
    return RedirectResponse("/summaries", status_code=303)


# --------------------------------------------------------------------------- settings
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = ""):
    with session_scope() as s:
        settings_store.ensure_defaults(s)
        prefs = settings_store.get_all(s)
        kws = list(s.scalars(select(Keyword).order_by(Keyword.group, Keyword.id)))
        return render(request, "settings.html", p=prefs, keywords=kws, saved=saved, platforms=PLATFORMS,
                      env_email_to=cfg.email_to, email_ready=cfg.email_enabled)


def _num(v: str):
    v = (v or "").strip().replace(",", "")
    return float(v) if v else None


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    with session_scope() as s:
        settings_store.put(s, "platforms", {p: form.get(f"platform_{p}") == "on" for p in PLATFORMS})
        interval = int(form.get("monitor_interval_minutes") or 60)
        settings_store.put(s, "monitor_interval_minutes", max(15, min(interval, 1440)))
        freq = form.get("notification_frequency")
        if freq in {"instant", "3h", "daily"}:
            settings_store.put(s, "notification_frequency", freq)
        settings_store.put(s, "email_enabled", form.get("email_enabled") == "on")
        settings_store.put(s, "email_to", (form.get("email_to") or "").strip())
        settings_store.put(s, "instant_min_score", max(0, min(100, int(form.get("instant_min_score") or 90))))
        settings_store.put(s, "min_relevance_score", max(0, min(100, int(form.get("min_relevance_score") or 60))))
        settings_store.put(s, "daily_summary_hour", max(0, min(23, int(form.get("daily_summary_hour") or 8))))
        settings_store.put(s, "digest_hour", max(0, min(23, int(form.get("digest_hour") or 9))))
        locs = [x.strip() for x in str(form.get("preferred_locations") or "").split(",") if x.strip()]
        settings_store.put(s, "preferred_locations", locs)
        settings_store.put(s, "remote_only", form.get("remote_only") == "on")
        settings_store.put(s, "salary_min_usd_annual", _num(str(form.get("salary_min_usd_annual") or "")))
        settings_store.put(s, "salary_min_hourly_usd", _num(str(form.get("salary_min_hourly_usd") or "")))
        settings_store.put(s, "max_job_age_days", max(1, min(120, int(form.get("max_job_age_days") or 30))))
        settings_store.put(s, "profile", str(form.get("profile") or "")[:5000])
        if "message_template" in form:
            settings_store.put(s, "applicant_name", str(form.get("applicant_name") or "").strip()[:200])
            for key in ("resume_url", "loom_url", "portfolio_url"):
                url = str(form.get(key) or "").strip()[:1000]
                settings_store.put(s, key, url if url.startswith(("https://", "http://")) else settings_store.DEFAULTS[key])
            settings_store.put(s, "message_template", str(form.get("message_template") or "").strip()[:8000]
                               or settings_store.DEFAULTS["message_template"])
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/keywords")
def keyword_add(text: str = Form(...), group: str = Form("ghl")):
    text = text.strip()[:200]
    if text:
        with session_scope() as s:
            if not s.scalar(select(Keyword).where(func.lower(Keyword.text) == text.lower())):
                s.add(Keyword(text=text, group="ghl" if group == "ghl" else "automation"))
    return RedirectResponse("/settings#keywords", status_code=303)


@app.post("/settings/keywords/{kid}/toggle")
def keyword_toggle(kid: int):
    with session_scope() as s:
        k = s.get(Keyword, kid)
        if k:
            k.enabled = not k.enabled
    return RedirectResponse("/settings#keywords", status_code=303)


@app.post("/settings/keywords/{kid}/delete")
def keyword_delete(kid: int):
    with session_scope() as s:
        k = s.get(Keyword, kid)
        if k:
            s.delete(k)
    return RedirectResponse("/settings#keywords", status_code=303)


@app.post("/settings/test-email")
def test_email():
    with session_scope() as s:
        ok = send_system_alert(s, "GHL Job Hunter test email", "Email delivery is working.")
    return RedirectResponse(f"/settings?saved={'email-ok' if ok else 'email-failed'}", status_code=303)


@app.post("/run-now")
def run_now():
    with session_scope() as s:
        settings_store.put(s, "run_requested", utcnow().isoformat())
    return RedirectResponse("/health?queued=1", status_code=303)


# --------------------------------------------------------------------------- health
@app.get("/health", response_class=HTMLResponse)
def health(request: Request, queued: str = ""):
    with session_scope() as s:
        now = utcnow()
        hb = settings_store.get(s, "worker_heartbeat")
        runs = list(s.scalars(select(MonitorRun).order_by(MonitorRun.id.desc()).limit(15)))
        failures = list(s.scalars(select(SearchLog).where(SearchLog.ok.is_(False)).order_by(SearchLog.id.desc()).limit(25)))
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        usage = s.execute(
            select(ApiUsage.provider, func.sum(ApiUsage.units), func.sum(ApiUsage.input_tokens),
                   func.sum(ApiUsage.output_tokens), func.sum(ApiUsage.cost_usd))
            .where(ApiUsage.at >= month_start).group_by(ApiUsage.provider)
        ).all()
        notes = list(s.scalars(select(Notification).order_by(Notification.id.desc()).limit(10)))
        last24 = s.execute(
            select(SearchLog.provider, func.count(SearchLog.id), func.sum(SearchLog.results),
                   func.sum(func.cast(SearchLog.ok.is_(False), type_=__import__("sqlalchemy").Integer)))
            .where(SearchLog.at >= now - timedelta(hours=24)).group_by(SearchLog.provider)
        ).all()
        days = max(1, (now - month_start).days + 1)
        total_cost = sum((u[4] or 0) for u in usage)
        return render(
            request, "health.html", hb=hb, hb_fresh=bool(hb) and now - datetime.fromisoformat(hb) < heartbeat_limit(s),
            worker_mode=settings_store.get(s, "worker_mode") or "not started",
            runs=runs, failures=failures, usage=usage, total_cost=total_cost,
            projected=total_cost / days * 30, notes=notes, last24=last24, coverage=platform_coverage(), cfg=cfg,
            queued=queued, interval=settings_store.get(s, "monitor_interval_minutes", 60),
        )


# --------------------------------------------------------------------------- JSON API
@app.get("/api/jobs")
def api_jobs(request: Request, limit: int = 50):
    f = dict(request.query_params)
    with session_scope() as s:
        jobs = list(s.scalars(_job_query(f).order_by(*SORTS["score"]).limit(min(limit, 500))))
        return [
            {
                "id": j.id, "title": j.title, "company": j.company, "score": j.score, "category": category_for(j.score),
                "location": j.location, "country": j.country, "work_arrangement": j.work_arrangement,
                "employment_type": j.employment_type, "salary": j.salary_text, "status": j.status,
                "tracker_status": j.tracker_status, "posted_at": j.posted_at.isoformat() if j.posted_at else None,
                "discovered_at": j.discovered_at.isoformat(), "url": j.primary_url,
                "sources": [{"platform": x.platform, "url": x.url} for x in j.sources],
                "summary": (j.analysis or {}).get("summary"),
            }
            for j in jobs
        ]
