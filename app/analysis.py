"""Claude-powered extraction, relevance scoring and advice for each job.

When ANTHROPIC_API_KEY is missing, a transparent keyword heuristic scores jobs instead
and every result is labelled as such, so nothing is presented as AI analysis that wasn't."""
from __future__ import annotations

import logging
import re
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .config import get_config
from .models import NOT_SPECIFIED

log = logging.getLogger(__name__)


def category_for(score: int | None) -> str | None:
    if score is None:
        return None
    if score >= 90:
        return "Excellent Match"
    if score >= 75:
        return "Strong Match"
    if score >= 60:
        return "Potential Match"
    return "Low Priority"


class SubScores(BaseModel):
    gohighlevel: int = Field(description="0-100: how central GoHighLevel/GHL/HighLevel/LeadConnector is")
    crm_implementation: int
    workflow_automation: int
    marketing_automation: int
    integrations: int = Field(description="Zapier, Make, Pabbly, n8n, webhooks, APIs")
    funnels_pipelines: int
    technical_implementation: int
    remote_friendly: int
    compensation: int = Field(description="0-100 vs the candidate's expectations; 50 when not disclosed")


class JobAnalysis(BaseModel):
    is_job_opportunity: bool = Field(
        description="True only if this is someone hiring/contracting for work. False for ads, courses, "
        "articles, service offers ('hire me'), or profiles."
    )
    appears_closed: bool = Field(description="True if the text says the role is filled, closed or expired")
    title: str = Field(description="Clean job title as stated. Never invent one.")
    company: str = Field(description="Hiring company or agency, or 'Not Specified'")
    summary: str = Field(description="2-4 sentence plain-English summary of the role")
    required_skills: list[str]
    required_experience: str = Field(description="e.g. '3+ years GHL', or 'Not Specified'")
    salary: str = Field(description="Salary or rate exactly as stated, or 'Not Specified'")
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: Literal["hour", "day", "week", "month", "year", "project"] | None = None
    employment_type: Literal["Full-time", "Part-time", "Contract", "Freelance", "Internship", "Not Specified"]
    work_arrangement: Literal["Remote", "Hybrid", "On-site", "Not Specified"]
    location: str = Field(description="City/region/country as stated, or 'Not Specified'")
    country: str = Field(description="Country name, 'Worldwide' for global remote, or 'Not Specified'")
    application_deadline: str = Field(description="As stated, or 'Not Specified'")
    application_method: str = Field(description="How to apply (link, email, DM, form), or 'Not Specified'")
    recruiter_info: str = Field(description="Publicly stated recruiter/hiring manager name or handle, or 'Not Specified'")
    sub_scores: SubScores
    relevance_score: int = Field(description="Overall 0-100 relevance to the candidate")
    matching_skills: list[str]
    missing_skills: list[str]
    red_flags: list[str] = Field(description="Scam signs, unpaid trials, vague pay, upfront fees, etc. Empty if none.")
    why_it_matches: str
    recommendation: Literal["Apply now", "Apply", "Consider", "Skip"]
    application_strategy: str = Field(description="3-5 concrete sentences on how to pitch for this role")


SYSTEM_PROMPT = """You evaluate job postings for a GoHighLevel and automation specialist.

Rules:
- Extract only what the posting states. When a field is not in the text, use exactly "Not Specified"
  (or null for numeric salary fields). Never guess company names, pay, dates or locations.
- Postings that explicitly mention GoHighLevel, GHL, HighLevel or LeadConnector deserve the highest
  scores when the work matches. Do not reject a job only because its title lacks "GHL": if the
  description clearly needs CRM implementation, workflow automation, integrations (Zapier, Make,
  Pabbly, n8n), funnels/pipelines or marketing automation, score it on that substance.
- Scoring bands: 90-100 excellent match, 75-89 strong, 60-74 potential, below 60 low priority.
- Social posts are often short. Score what is there; don't penalise brevity, but do flag missing pay or
  company as a red flag only when it matters (e.g. "DM for details" with no company).
- If the text is not a hiring post (an ad, course, article, someone offering their own services),
  set is_job_opportunity=false and relevance_score=0."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=get_config().anthropic_api_key, max_retries=3, timeout=120)


def _supports_fallbacks(model: str) -> bool:
    return model in {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1"}


def call_claude_structured(system: str, user: str, schema: type[BaseModel], max_tokens: int = 8000):
    """Returns (parsed, usage). Raises on API errors or refusals."""
    cfg = get_config()
    kwargs: dict = dict(
        model=cfg.claude_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=schema,
        output_config={"effort": cfg.claude_effort},
    )
    client = _client()
    if _supports_fallbacks(cfg.claude_model):
        # Server-side fallback: if a safety classifier declines, the API retries on a suitable model.
        resp = client.beta.messages.parse(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    else:
        resp = client.messages.parse(**kwargs)
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to analyse this posting")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise RuntimeError(f"Claude response incomplete (stop_reason={resp.stop_reason})")
    return resp.parsed_output, resp.usage


def build_user_prompt(job: dict, profile: str, prefs: dict) -> str:
    desc = (job.get("description") or "")[:12000]
    lines = [
        "<candidate_profile>", profile.strip(), "</candidate_profile>",
        "<candidate_preferences>",
        f"Preferred locations: {', '.join(prefs.get('preferred_locations') or []) or 'Any'}",
        f"Remote only: {bool(prefs.get('remote_only'))}",
        f"Minimum annual salary (USD): {prefs.get('salary_min_usd_annual') or 'not set'}",
        f"Minimum hourly rate (USD): {prefs.get('salary_min_hourly_usd') or 'not set'}",
        "</candidate_preferences>",
        "<posting>",
        f"Source platform: {job.get('platform')}",
        f"URL: {job.get('url')}",
        f"Title as listed: {job.get('title')}",
        f"Company as listed: {job.get('company') or 'Not Specified'}",
        f"Location as listed: {job.get('location') or 'Not Specified'}",
        f"Salary as listed: {job.get('salary_text') or 'Not Specified'}",
        f"Employment type as listed: {job.get('employment_type') or 'Not Specified'}",
        f"Posted: {job.get('posted_text') or 'Not Specified'}",
        "Text:",
        desc or "(no description available; only the title)",
        "</posting>",
    ]
    if job.get("is_snippet"):
        lines.append("Note: the text is only a search-result snippet, not the full posting.")
    return "\n".join(lines)


def analyze_with_claude(job: dict, profile: str, prefs: dict) -> tuple[JobAnalysis, int, int]:
    parsed, usage = call_claude_structured(SYSTEM_PROMPT, build_user_prompt(job, profile, prefs), JobAnalysis)
    parsed.relevance_score = max(0, min(100, int(parsed.relevance_score)))
    return parsed, usage.input_tokens, usage.output_tokens


# ---------------------------------------------------------------------------
# Keyword heuristic used only when Claude is not configured (clearly labelled)
# ---------------------------------------------------------------------------
_WEIGHTS = [
    (r"\b(go\s?high\s?level|gohighlevel|highlevel|lead\s?connector)\b|\bghl\b", 45),
    (r"\bcrm\b", 10), (r"\bworkflows?\b", 8), (r"\bautomation\b", 10), (r"\bmarketing automation\b", 8),
    (r"\b(zapier|make\.com|integromat|pabbly|n8n)\b", 10), (r"\b(funnels?|pipelines?)\b", 6),
    (r"\b(api|webhooks?|integrations?)\b", 5), (r"\bremote\b", 4),
]


def heuristic_score(text: str) -> int:
    score = 0
    for pattern, weight in _WEIGHTS:
        if re.search(pattern, text or "", re.I):
            score += weight
    return min(score, 89)  # never claim an "Excellent" match without Claude reading it


def heuristic_analysis(job: dict) -> dict:
    text = f"{job.get('title')} {job.get('description')}"
    score = heuristic_score(text)
    found = sorted({m.group(0) for p, _ in _WEIGHTS for m in re.finditer(p, text, re.I)}, key=str.lower)
    return {
        "relevance_score": score,
        "summary": NOT_SPECIFIED,
        "matching_skills": found,
        "missing_skills": [],
        "red_flags": [],
        "why_it_matches": "Scored by keyword matching because Claude is not configured.",
        "recommendation": "Consider" if score >= 60 else "Skip",
        "application_strategy": NOT_SPECIFIED,
        "sub_scores": {},
    }
