"""One monitoring run: fetch -> filter -> enrich -> dedupe/store -> analyse -> notify."""
from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings_store
from .analysis import analyze_with_claude, category_for, heuristic_analysis
from .config import get_config
from .dedupe import find_duplicate_job, find_source
from .enrich import fetch_page_info
from .models import NOT_SPECIFIED, ApiUsage, Job, JobSource, MonitorRun, SearchLog, utcnow
from .normalize import canonical_url, content_hash, is_relevant, norm_company, norm_title
from .sources import FetchContext, RawJob, Source, all_sources

log = logging.getLogger(__name__)

ANNUAL_FACTOR = {"hour": 2080, "day": 260, "week": 52, "month": 12, "year": 1}


def annual_usd(lo: float | None, hi: float | None, currency: str | None, period: str | None) -> float | None:
    if not (currency or "").upper() == "USD" or period not in ANNUAL_FACTOR:
        return None  # no FX guessing: only USD amounts feed the averages
    vals = [v for v in (lo, hi) if v]
    if not vals:
        return None
    return round(sum(vals) / len(vals) * ANNUAL_FACTOR[period])


def _v(value: str | None) -> str:
    value = (value or "").strip()
    return value if value else NOT_SPECIFIED


class RunRecorder:
    def __init__(self, session: Session, run: MonitorRun):
        self.session, self.run = session, run

    def log_search(self, provider, platform, query, ok, results, error):
        self.session.add(SearchLog(run_id=self.run.id, provider=provider, platform=platform, query=query[:1000],
                                   ok=ok, results=results, error=(error or None) and str(error)[:2000]))

    def record_usage(self, provider, units, cost):
        self.session.add(ApiUsage(provider=provider, kind="search", units=units, cost_usd=float(cost) * units))


def _source_due(src: Source, last_runs: dict, now: datetime) -> bool:
    if not src.min_interval_minutes:
        return True
    last = last_runs.get(src.name)
    return not last or now - datetime.fromisoformat(last) >= timedelta(minutes=src.min_interval_minutes)


def collect(session: Session, run: MonitorRun, sources: list[Source] | None = None, *, force_all: bool = False) -> tuple[list[RawJob], dict]:
    prefs = settings_store.get_all(session)
    kws = settings_store.enabled_keywords(session)
    rotation = settings_store.get(session, "rotation", {}) or {}
    last_runs = settings_store.get(session, "source_last_run", {}) or {}
    last_weekly = settings_store.get(session, "last_weekly_sweep")
    now = utcnow()
    weekly = not last_weekly or now - datetime.fromisoformat(last_weekly) >= timedelta(hours=24)
    rec = RunRecorder(session, run)
    cfg = get_config()
    ctx = FetchContext(
        keywords=[k.text for k in sorted(kws, key=lambda k: k.group != "ghl")],
        ghl_keywords=[k.text for k in kws if k.group == "ghl"],
        automation_keywords=[k.text for k in kws if k.group != "ghl"],
        enabled_platforms={p for p, on in prefs["platforms"].items() if on},
        rotation=rotation, log_search=rec.log_search, record_usage=rec.record_usage,
        query_budget={"search": cfg.max_search_queries_per_run, "google_jobs": 2},
        last_run=last_runs, now=now, weekly_sweep=weekly,
    )
    stats = {"sources": {}, "skipped_sources": {}}
    raws: list[RawJob] = []
    for src in sources if sources is not None else all_sources():
        if not any(p in ctx.enabled_platforms for p in src.platforms):
            stats["skipped_sources"][src.name] = "platform disabled"
            continue
        ok, why = src.available()
        if not ok:
            stats["skipped_sources"][src.name] = why
            continue
        if not force_all and not _source_due(src, last_runs, now):
            stats["skipped_sources"][src.name] = "polled recently"
            continue
        try:
            got = src.fetch(ctx)
            last_runs[src.name] = now.isoformat()
            stats["sources"][src.name] = len(got)
            raws.extend(got)
        except Exception as exc:
            log.exception("source %s failed", src.name)
            rec.log_search(src.name, ",".join(src.platforms), "", False, 0, f"{exc}")
            stats["sources"][src.name] = f"error: {exc}"
    settings_store.put(session, "rotation", ctx.rotation)
    settings_store.put(session, "source_last_run", last_runs)
    if weekly:
        settings_store.put(session, "last_weekly_sweep", now.isoformat())
    return raws, stats


def ingest(session: Session, raws: list[RawJob], stats: dict) -> list[Job]:
    """Store new jobs, merge duplicates and refresh known ones. Returns jobs needing analysis."""
    cfg = get_config()
    prefs = settings_store.get_all(session)
    max_age = timedelta(days=int(prefs.get("max_job_age_days") or 30))
    now = utcnow()
    new_jobs: list[Job] = []
    counters = dict(raw=len(raws), new=0, merged=0, refreshed=0, irrelevant=0, expired=0, enriched=0, no_url=0)
    enrich_budget = cfg.max_enrich_fetches_per_run
    seen_this_run: set[str] = set()

    for r in raws:
        if not r.url or not r.url.startswith("http"):
            counters["no_url"] += 1
            continue
        curl = canonical_url(r.url)
        if curl in seen_this_run:
            continue
        seen_this_run.add(curl)

        src = find_source(session, r.url, r.platform_post_id)
        if src:
            src.last_seen_at = now
            job = src.job
            job.last_seen_at = now
            if job.status == "New" and job.discovered_at < now - timedelta(hours=24):
                job.status = "Active"
            if r.description and not r.is_snippet:
                h = content_hash(r.title, r.description)
                if job.content_hash and h != job.content_hash:
                    job.description, job.content_hash, job.content_updated_at = r.description, h, now
            counters["refreshed"] += 1
            continue

        if not is_relevant(f"{r.title} {r.description} {r.company}"):
            counters["irrelevant"] += 1
            continue

        # enrich thin results from the public job page (non-social sites only)
        if (r.is_snippet or not r.description or len(r.description) < 400) and enrich_budget > 0:
            enrich_budget -= 1
            info = fetch_page_info(r.url)
            if info:
                counters["enriched"] += 1
                if info.closed:
                    counters["expired"] += 1
                    continue
                r.title = info.title or r.title
                r.company = r.company or info.company
                if info.description and len(info.description) > len(r.description or ""):
                    r.description, r.is_snippet = info.description, False
                r.location = r.location or info.location
                r.posted_at = r.posted_at or info.posted_at
                r.deadline_at = r.deadline_at or info.deadline_at
                r.salary_text = r.salary_text or info.salary_text
                r.employment_type = r.employment_type or info.employment_type
                r.work_arrangement = r.work_arrangement or info.work_arrangement

        if r.deadline_at and r.deadline_at < now:
            counters["expired"] += 1
            continue
        if r.posted_at and r.posted_at < now - max_age and not (r.deadline_at and r.deadline_at > now):
            counters["expired"] += 1
            continue

        job = find_duplicate_job(session, r.title, r.company, r.description)
        if job:
            counters["merged"] += 1
            job.last_seen_at = now
            # keep the richest description and fill blanks from the new source
            if r.description and len(r.description) > len(job.description or "") and not r.is_snippet:
                job.description = r.description
            if job.salary_text == NOT_SPECIFIED and r.salary_text:
                job.salary_text = r.salary_text
        else:
            job = Job(
                title=r.title[:500] or NOT_SPECIFIED, company=_v(r.company)[:300], description=r.description or "",
                location=_v(r.location)[:300], salary_text=_v(r.salary_text)[:300],
                employment_type=_v(r.employment_type)[:50], work_arrangement=_v(r.work_arrangement)[:50],
                posted_at=r.posted_at, posted_at_text=(r.posted_text or None) and str(r.posted_text)[:100],
                deadline_at=r.deadline_at,
                deadline=r.deadline_at.strftime("%Y-%m-%d") if r.deadline_at else NOT_SPECIFIED,
                application_method=_v(r.application_method)[:500], recruiter_info=_v(r.recruiter_info)[:500],
                primary_url=r.url[:2000], primary_platform=r.platform,
                norm_title=norm_title(r.title)[:500], norm_company=norm_company(r.company)[:300],
                content_hash=content_hash(r.title, r.description), discovered_at=now, last_seen_at=now,
                status="New",
            )
            session.add(job)
            session.flush()
            new_jobs.append(job)
            job._raw = r  # type: ignore[attr-defined]  # transient: used by analysis in this run
            counters["new"] += 1
        for u in [r.url, *r.extra_urls]:
            cu = canonical_url(u)
            if session.scalar(select(JobSource.id).where(JobSource.canonical_url == cu)):
                continue
            session.add(JobSource(
                job_id=job.id, platform=r.platform, provider=r.provider, url=u[:2000], canonical_url=cu[:2000],
                platform_post_id=r.platform_post_id if u == r.url else None, query=(r.query or "")[:500],
                first_seen_at=now, last_seen_at=now, raw=r.raw if u == r.url else {},
            ))
        session.flush()
    stats.update(counters)
    return new_jobs


def apply_analysis(job: Job, a, by: str) -> None:
    job.analysis = {
        "summary": a.summary, "matching_skills": a.matching_skills, "missing_skills": a.missing_skills,
        "red_flags": a.red_flags, "why_it_matches": a.why_it_matches, "recommendation": a.recommendation,
        "application_strategy": a.application_strategy, "sub_scores": a.sub_scores.model_dump(),
    }
    job.is_job_posting = a.is_job_opportunity
    job.score = a.relevance_score if a.is_job_opportunity else 0
    job.category = category_for(job.score)
    if a.appears_closed:
        job.status = "Closed"
    # fill only fields we don't already have from structured sources
    if a.title and a.title != NOT_SPECIFIED and len(job.title) > 120:
        job.title = a.title[:500]
    for field, val in [
        ("company", a.company), ("location", a.location), ("salary_text", a.salary),
        ("employment_type", a.employment_type), ("work_arrangement", a.work_arrangement),
        ("application_method", a.application_method), ("recruiter_info", a.recruiter_info),
        ("required_experience", a.required_experience),
    ]:
        if getattr(job, field) in (None, "", NOT_SPECIFIED) and val:
            setattr(job, field, str(val)[:500])
    if job.norm_company == "" and job.company != NOT_SPECIFIED:
        job.norm_company = norm_company(job.company)
    job.country = (a.country or NOT_SPECIFIED)[:100]
    if job.deadline == NOT_SPECIFIED and a.application_deadline:
        job.deadline = a.application_deadline[:100]
    job.required_skills = a.required_skills[:30]
    job.salary_min, job.salary_max = a.salary_min, a.salary_max
    job.salary_currency, job.salary_period = a.salary_currency, a.salary_period
    job.salary_usd_annual = annual_usd(a.salary_min, a.salary_max, a.salary_currency, a.salary_period)
    job.analyzed_by, job.analyzed_at, job.analysis_error = by, utcnow(), None


def analyze_jobs(session: Session, jobs: list[Job], stats: dict) -> None:
    cfg = get_config()
    prefs = settings_store.get_all(session)
    done = errors = 0
    for job in jobs[: cfg.max_analyses_per_run] if cfg.claude_enabled else jobs:
        raw: RawJob | None = getattr(job, "_raw", None)
        payload = {
            "platform": job.primary_platform, "url": job.primary_url, "title": job.title,
            "company": job.company if job.company != NOT_SPECIFIED else None, "location": job.location,
            "salary_text": job.salary_text, "employment_type": job.employment_type,
            "posted_text": job.posted_at_text or (job.posted_at.isoformat() if job.posted_at else None),
            "description": job.description, "is_snippet": bool(raw and raw.is_snippet),
        }
        if cfg.claude_enabled:
            try:
                a, tin, tout = analyze_with_claude(payload, prefs.get("profile") or "", prefs)
                session.add(ApiUsage(provider="claude", kind="job_analysis", input_tokens=tin, output_tokens=tout,
                                     cost_usd=(tin * cfg.claude_input_price + tout * cfg.claude_output_price) / 1e6))
                apply_analysis(job, a, cfg.claude_model)
                done += 1
            except Exception as exc:
                log.warning("Claude analysis failed for %s: %s", job.id, exc)
                job.analysis_error = str(exc)[:2000]
                errors += 1
        else:
            h = heuristic_analysis(payload)
            job.analysis = h
            job.score = h["relevance_score"]
            job.category = category_for(job.score)
            job.analyzed_by, job.analyzed_at = "keyword heuristic", utcnow()
            done += 1
        session.flush()
    stats["analyzed"], stats["analysis_errors"] = done, errors


def retry_unanalyzed(session: Session, limit: int = 20) -> list[Job]:
    """Jobs whose analysis failed earlier (or were over the per-run cap) get another try."""
    from sqlalchemy import or_

    recent = utcnow() - timedelta(hours=6)
    return list(session.scalars(
        select(Job)
        .where(Job.analyzed_at.is_(None), or_(Job.analysis_error.is_(None), Job.discovered_at >= recent))
        .order_by(Job.discovered_at.desc()).limit(limit)
    ))


def run_monitor(session: Session, trigger: str = "schedule", sources: list[Source] | None = None) -> MonitorRun:
    from .notify import send_instant_alerts

    run = MonitorRun(trigger=trigger)
    session.add(run)
    session.flush()
    stats: dict = {}
    try:
        raws, stats = collect(session, run, sources, force_all=(trigger == "manual"))
        new_jobs = ingest(session, raws, stats)
        pending = new_jobs + [j for j in retry_unanalyzed(session) if j not in new_jobs]
        analyze_jobs(session, pending, stats)
        stats["emailed"] = send_instant_alerts(session)
        failures = session.scalar(
            select(SearchLog.id).where(SearchLog.run_id == run.id, SearchLog.ok.is_(False)).limit(1)
        )
        run.status = "partial" if failures or stats.get("analysis_errors") else "ok"
    except Exception as exc:
        log.exception("monitor run failed")
        run.status, run.error = "failed", "".join(traceback.format_exception(exc))[-4000:]
    run.stats, run.finished_at = stats, utcnow()
    settings_store.put(session, "last_run_finished_at", run.finished_at.isoformat())
    session.flush()
    return run


def maintenance(session: Session) -> dict:
    """Daily: mark stale or past-deadline listings expired, recheck a few URLs for closure."""
    now = utcnow()
    max_age = timedelta(days=int(settings_store.get(session, "max_job_age_days", 30)))
    changed = 0
    for job in session.scalars(select(Job).where(Job.status.in_(["New", "Active"]))):
        if job.deadline_at and job.deadline_at < now:
            job.status, changed = "Expired", changed + 1
        elif job.status == "New" and job.discovered_at < now - timedelta(hours=24):
            job.status = "Active"
        elif job.last_seen_at < now - max_age and (not job.posted_at or job.posted_at < now - max_age):
            job.status, changed = "Expired", changed + 1
    # recheck recent active, non-social listings for 404/closed text
    rechecked = 0
    for job in session.scalars(
        select(Job).where(Job.status == "Active", Job.primary_platform.in_(["Google", "Job Boards"]))
        .order_by(Job.last_seen_at.asc()).limit(30)
    ):
        info = fetch_page_info(job.primary_url)
        rechecked += 1
        if info and info.closed:
            job.status, changed = "Closed", changed + 1
    session.flush()
    return {"expired_or_closed": changed, "rechecked": rechecked}
