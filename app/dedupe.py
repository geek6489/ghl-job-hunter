"""Duplicate detection: URL, platform post id, normalised title + company, description similarity."""
from __future__ import annotations

from datetime import timedelta

from rapidfuzz import fuzz
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import Job, JobSource, utcnow
from .normalize import canonical_url, norm_company, norm_text, norm_title

DESC_SIMILARITY = 88  # token_set_ratio threshold for "same posting, different site"
TITLE_SIMILARITY = 90
LOOKBACK_DAYS = 60


def find_source(session: Session, url: str, post_id: str | None) -> JobSource | None:
    c = canonical_url(url)
    src = session.scalar(select(JobSource).where(JobSource.canonical_url == c))
    if src:
        return src
    if post_id:
        return session.scalar(select(JobSource).where(JobSource.platform_post_id == post_id).limit(1))
    return None


def find_duplicate_job(session: Session, title: str, company: str | None, description: str | None) -> Job | None:
    """Same job seen elsewhere: identical normalised title+company, or very similar
    title and description (for reposts on another platform)."""
    nt, nc = norm_title(title), norm_company(company)
    if not nt:
        return None
    since = utcnow() - timedelta(days=LOOKBACK_DAYS)
    if nc:
        exact = session.scalar(
            select(Job).where(Job.norm_title == nt, Job.norm_company == nc, Job.discovered_at >= since).limit(1)
        )
        if exact:
            return exact
    desc = norm_text(description)[:3000]
    if len(desc) < 200:
        return None  # snippets are too short to compare safely
    # candidates: same company, or a title that shares its first words
    first = " ".join(nt.split()[:2])
    conds = [Job.norm_title.like(f"{first}%")]
    if nc:
        conds.append(Job.norm_company == nc)
    candidates = session.scalars(
        select(Job).where(Job.discovered_at >= since, or_(*conds)).limit(200)
    )
    for job in candidates:
        if nc and job.norm_company and job.norm_company != nc:
            continue
        if fuzz.token_set_ratio(nt, job.norm_title) < TITLE_SIMILARITY:
            continue
        other = norm_text(job.description)[:3000]
        if len(other) >= 200 and fuzz.token_set_ratio(desc, other) >= DESC_SIMILARITY:
            return job
    return None
