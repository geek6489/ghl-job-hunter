"""Daily job-hunting summary: numbers computed from the database, narrative written by Claude."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings_store
from .analysis import call_claude_structured
from .config import get_config
from .models import ApiUsage, DailySummary, Job, utcnow
from .notify import _deliver, _env, local_now

log = logging.getLogger(__name__)


class SummaryNarrative(BaseModel):
    headline: str
    overview: str
    hiring_trends: list[str]
    top_pick_reasoning: str
    advice_for_today: list[str]


def compute_stats(session: Session, since, until) -> dict:
    jobs = list(session.scalars(
        select(Job).where(Job.discovered_at >= since, Job.discovered_at < until, Job.is_job_posting.is_(True))
    ))
    scored = [j for j in jobs if j.score is not None]
    salaries = [j.salary_usd_annual for j in jobs if j.salary_usd_annual]
    platforms: Counter = Counter()
    for j in jobs:
        for p in j.platforms:
            platforms[p] += 1
    best = sorted(scored, key=lambda j: (j.score or 0), reverse=True)[:5]
    avg = round(sum(salaries) / len(salaries)) if salaries else None
    return {
        "total_new": len(jobs),
        "excellent": sum(1 for j in scored if j.score >= 90),
        "strong": sum(1 for j in scored if 75 <= j.score < 90),
        "potential": sum(1 for j in scored if 60 <= j.score < 75),
        "avg_salary_usd_annual": avg,
        "avg_salary_text": f"${avg:,}/yr" if avg else "Not enough data",
        "salary_sample_size": len(salaries),
        "platforms": platforms.most_common(),
        "remote_share": round(100 * sum(1 for j in jobs if j.work_arrangement == "Remote") / len(jobs)) if jobs else 0,
        "employment_types": Counter(j.employment_type for j in jobs).most_common(),
        "ghl_explicit": sum(1 for j in jobs if (j.analysis or {}).get("sub_scores", {}).get("gohighlevel", 0) >= 70),
        "best": [
            {"id": j.id, "title": j.title, "company": j.company, "score": j.score, "url": j.primary_url,
             "location": j.location, "salary": j.salary_text, "platforms": ", ".join(j.platforms),
             "summary": (j.analysis or {}).get("summary", "")}
            for j in best
        ],
    }


def _fallback_narrative(st: dict) -> str:
    plats = ", ".join(f"{p} ({n})" for p, n in st["platforms"][:3]) or "none"
    return (
        f"{st['total_new']} new opportunities in the last 24 hours: {st['excellent']} excellent, "
        f"{st['strong']} strong and {st['potential']} potential matches. Most active platforms: {plats}. "
        f"{st['remote_share']}% were remote.\n\n(Claude is not configured, so this summary has no AI commentary.)"
    )


def generate_daily_summary(session: Session, force: bool = False) -> DailySummary | None:
    local = local_now()
    day = local.strftime("%Y-%m-%d")
    existing = session.scalar(select(DailySummary).where(DailySummary.day == day))
    if existing and not force:
        return existing
    until = utcnow()
    since = until - timedelta(hours=24)
    st = compute_stats(session, since, until)
    cfg = get_config()
    narrative, by = _fallback_narrative(st), "statistics only"
    if cfg.claude_enabled:
        prompt = (
            "Write today's job-hunting summary for a GoHighLevel and automation specialist, using only these "
            "numbers and listings (do not invent any):\n"
            f"{st}\n\nKeep it concise and practical. Trends should be grounded in the data above; if the sample "
            "is small, say so instead of overgeneralising."
        )
        try:
            parsed, usage = call_claude_structured(
                "You are a concise career assistant. Never invent facts beyond the data provided.", prompt,
                SummaryNarrative, max_tokens=6000,
            )
            session.add(ApiUsage(provider="claude", kind="daily_summary", input_tokens=usage.input_tokens,
                                 output_tokens=usage.output_tokens,
                                 cost_usd=(usage.input_tokens * cfg.claude_input_price + usage.output_tokens * cfg.claude_output_price) / 1e6))
            narrative = (
                f"{parsed.headline}\n\n{parsed.overview}\n\nTrends:\n" + "\n".join(f"- {t}" for t in parsed.hiring_trends)
                + f"\n\nTop pick: {parsed.top_pick_reasoning}\n\nToday:\n" + "\n".join(f"- {a}" for a in parsed.advice_for_today)
            )
            by = cfg.claude_model
        except Exception as exc:
            log.exception("daily summary via Claude failed")
            narrative += f"\n\n(Claude summary failed: {exc})"
    summary = existing or DailySummary(day=day)
    summary.stats, summary.narrative, summary.generated_by, summary.created_at = st, narrative, by, utcnow()
    if not existing:
        session.add(summary)
    session.flush()
    return summary


def email_summary(session: Session, summary: DailySummary) -> bool:
    if not settings_store.get(session, "email_enabled", True):
        return False
    html = _env.get_template("summary.html").render(s=summary, st=summary.stats, base_url=get_config().public_base_url)
    subj = f"Daily GHL job summary {summary.day}: {summary.stats.get('total_new', 0)} new, {summary.stats.get('excellent', 0)} excellent"
    ok = _deliver(session, "daily_summary", subj, html, summary.narrative, [])
    if ok:
        summary.emailed_at = utcnow()
    return ok


def daily_summary_due(session: Session) -> bool:
    hour = int(settings_store.get(session, "daily_summary_hour", 8))
    local = local_now()
    if local.hour < hour:
        return False
    s = session.scalar(select(DailySummary).where(DailySummary.day == local.strftime("%Y-%m-%d")))
    return s is None
