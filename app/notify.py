"""Instant alerts, 3-hourly / daily digests, and system alerts. Every email is logged and
each job is marked once sent, so nothing is emailed twice."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings_store
from .config import get_config
from .emailer import EmailNotConfigured, send_email
from .models import Job, Notification, utcnow

log = logging.getLogger(__name__)

_env = Environment(
    loader=FileSystemLoader(__file__.rsplit("/", 1)[0] + "/email_templates"),
    autoescape=select_autoescape(["html"]),
)


def local_now() -> datetime:
    return datetime.now(ZoneInfo(get_config().timezone))


def fmt_local(dt: datetime | None) -> str:
    if not dt:
        return "Not Specified"
    from datetime import timezone

    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(get_config().timezone)).strftime("%d %b %Y, %H:%M")


_env.filters["local"] = fmt_local


def recipients(session: Session) -> list[str]:
    raw = settings_store.get(session, "email_to") or get_config().email_to
    return [a.strip() for a in str(raw).replace(";", ",").split(",") if a.strip()]


def passes_preferences(job: Job, prefs: dict) -> bool:
    if prefs.get("remote_only") and job.work_arrangement in ("On-site", "Hybrid"):
        return False
    floor = prefs.get("salary_min_usd_annual")
    if floor and job.salary_usd_annual and job.salary_usd_annual < float(floor):
        return False
    hourly = prefs.get("salary_min_hourly_usd")
    if hourly and job.salary_usd_annual and job.salary_period == "hour" and job.salary_max and job.salary_currency == "USD":
        if job.salary_max < float(hourly):
            return False
    return True


def _deliver(session: Session, kind: str, subject: str, html: str, text: str, jobs: list[Job]) -> bool:
    note = Notification(kind=kind, subject=subject, job_ids=[j.id for j in jobs])
    session.add(note)
    try:
        send_email(recipients(session), subject, html, text)
        note.ok = True
        return True
    except EmailNotConfigured as exc:
        note.ok, note.error = False, str(exc)
        log.warning("email skipped: %s", exc)
        return False
    except Exception as exc:
        note.ok, note.error = False, str(exc)[:1000]
        log.exception("email send failed")
        return False
    finally:
        session.flush()


def render_jobs_email(title: str, intro: str, jobs: list[Job]) -> tuple[str, str]:
    base = get_config().public_base_url
    html = _env.get_template("jobs.html").render(title=title, intro=intro, jobs=jobs, base_url=base)
    lines = [title, "", intro, ""]
    for j in jobs:
        lines += [
            f"[{j.score}] {j.title} - {j.company}",
            f"  {j.location} | {j.work_arrangement} | {j.employment_type} | {j.salary_text}",
            f"  Source: {', '.join(j.platforms)} | Apply: {j.primary_url}",
            "",
        ]
    lines.append(f"Dashboard: {base}/jobs")
    return html, "\n".join(lines)


def _eligible(session: Session, min_score: int) -> list[Job]:
    since = utcnow() - timedelta(days=7)
    q = (
        select(Job)
        .where(
            Job.is_job_posting.is_(True), Job.score >= min_score, Job.status.in_(["New", "Active"]),
            Job.discovered_at >= since, Job.notified_instant_at.is_(None), Job.notified_digest_at.is_(None),
            Job.tracker_status != "Not Interested",
        )
        .order_by(Job.score.desc(), Job.discovered_at.desc())
    )
    return list(session.scalars(q))


def send_instant_alerts(session: Session) -> int:
    """Called after every monitoring run."""
    prefs = settings_store.get_all(session)
    if not prefs.get("email_enabled", True):
        return 0
    excellent_floor = int(prefs.get("instant_min_score") or 90)
    sent = 0
    excellent = [j for j in _eligible(session, excellent_floor) if passes_preferences(j, prefs)]
    if excellent:
        subj = (f"Excellent match ({excellent[0].score}): {excellent[0].title} at {excellent[0].company}"
                if len(excellent) == 1 else f"{len(excellent)} excellent GHL job matches found")
        html, text = render_jobs_email("Excellent matches", "Found in the latest hourly scan. Apply early.", excellent)
        if _deliver(session, "instant", subj, html, text, excellent):
            now = utcnow()
            for j in excellent:
                j.notified_instant_at = now
            sent += len(excellent)
    if prefs.get("notification_frequency") == "instant":
        sent += send_digest(session, force=True, kind="instant")
    return sent


def digest_due(session: Session) -> bool:
    prefs = settings_store.get_all(session)
    freq = prefs.get("notification_frequency")
    last = settings_store.get(session, "last_digest_at")
    last_dt = datetime.fromisoformat(last) if last else None
    now = utcnow()
    if freq == "3h":
        return last_dt is None or now - last_dt >= timedelta(hours=3)
    if freq == "daily":
        local = local_now()
        if local.hour < int(prefs.get("digest_hour", 9)):
            return False
        return last_dt is None or fmt_local(last_dt)[:11] != local.strftime("%d %b %Y")
    return False


def send_digest(session: Session, force: bool = False, kind: str = "digest") -> int:
    prefs = settings_store.get_all(session)
    if not prefs.get("email_enabled", True):
        return 0
    if not force and not digest_due(session):
        return 0
    min_score = int(prefs.get("min_relevance_score") or 60)
    jobs = [j for j in _eligible(session, min_score) if passes_preferences(j, prefs)]
    settings_store.put(session, "last_digest_at", utcnow().isoformat())
    if not jobs:
        return 0
    excellent = sum(1 for j in jobs if (j.score or 0) >= 90)
    strong = sum(1 for j in jobs if 75 <= (j.score or 0) < 90)
    subj = f"{len(jobs)} new GHL & automation jobs ({excellent} excellent, {strong} strong)"
    label = {"instant": "New matches from the latest scan", "digest": "Your job digest"}[kind]
    html, text = render_jobs_email(label, f"{len(jobs)} relevant opportunities you haven't been sent yet.", jobs[:40])
    if _deliver(session, kind, subj, html, text, jobs[:40]):
        now = utcnow()
        for j in jobs[:40]:
            j.notified_digest_at = now
        return len(jobs[:40])
    return 0


def send_system_alert(session: Session, subject: str, body: str) -> bool:
    html = _env.get_template("alert.html").render(subject=subject, body=body, base_url=get_config().public_base_url)
    return _deliver(session, "alert", subject, html, body, [])
