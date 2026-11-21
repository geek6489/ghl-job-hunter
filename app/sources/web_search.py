"""Search-engine coverage for Google, LinkedIn, X, Facebook and Instagram.

Uses a licensed search API (Serper.dev's Google results, or Brave Search API) with
`site:` filters, so we only read what the search engine has already indexed publicly.
No platform is scraped directly."""
from __future__ import annotations

import logging
import re

from ..config import get_config
from ..http import get_json, post_json
from ..normalize import HIRING_TERMS, detect_platform, parse_date, platform_post_id
from .base import FetchContext, RawJob, Source, chunk_or_groups, take_rotating

log = logging.getLogger(__name__)

SOCIAL_EXCLUDES = "-site:linkedin.com -site:facebook.com -site:instagram.com -site:x.com -site:twitter.com"

# platform -> list of query templates; {kw} is an OR-group of keyword phrases
TEMPLATES: dict[str, list[str]] = {
    "Google": [
        "{kw} (job OR jobs OR hiring OR careers OR apply) " + SOCIAL_EXCLUDES,
    ],
    "LinkedIn": [
        "site:linkedin.com/jobs/view {kw}",
        "site:linkedin.com/posts {kw} (hiring OR \"looking for\" OR \"we're hiring\")",
    ],
    "X": [
        "(site:x.com OR site:twitter.com) {kw} (hiring OR \"looking for\" OR freelance)",
    ],
    "Facebook": [
        "site:facebook.com {kw} (hiring OR \"looking for\" OR \"need a\")",
    ],
    "Instagram": [
        "site:instagram.com {kw} (hiring OR \"we're hiring\" OR \"looking for\")",
    ],
}

# A search result that is an article, course or profile rather than a job.
NOT_A_JOB = re.compile(
    r"\b(course|tutorial|review|reviews|pricing|vs\.?|alternative|alternatives|how to|what is|guide|"
    r"certification|affiliate|coupon|webinar|template|snapshot for sale|"
    r"hire me|i am a|i'm a|my services|available for hire|top \d+)\b",
    re.I,
)
JOB_BOARD_PATH = re.compile(r"/(jobs?|careers?|positions?|openings?|vacanc|apply|job-board|hiring)", re.I)


def build_queries(ctx: FetchContext) -> list[tuple[str, str]]:
    """All (platform, query) pairs for enabled platforms, GHL keyword groups first."""
    groups = chunk_or_groups(ctx.ghl_keywords, 4) + chunk_or_groups(ctx.automation_keywords, 4)
    plan: list[tuple[str, str]] = []
    for kw in groups:
        for platform, templates in TEMPLATES.items():
            if platform not in ctx.enabled_platforms:
                continue
            for tpl in templates:
                plan.append((platform, tpl.format(kw=kw)))
    return plan


FREE_FLAG = "serper_free_plan"
FREE_PLAN_ERROR = "not allowed for free accounts"
FREE_QUERIES_PER_RUN = 8  # Serper's free credits are limited: about 190 a day at this rate
PLAIN_TEMPLATES = ["{kw} job", "{kw} hiring", "{kw} remote job", "{kw} linkedin hiring", "{kw} freelance hiring"]


def build_plain_queries(ctx: FetchContext) -> list[tuple[str, str]]:
    """Queries with no search operators (no site:, OR, quotes or minus), for Serper's free plan.
    The platform is worked out from each result's URL. GHL keywords come first."""
    seen, plan = set(), []
    for kw in list(ctx.ghl_keywords) + list(ctx.automation_keywords):
        k = kw.strip().strip('"').lower()
        if k and k not in seen:
            seen.add(k)
            plan.extend(("Google", tpl.format(kw=k)) for tpl in PLAIN_TEMPLATES[:2])
    for kw in list(ctx.ghl_keywords)[:3]:
        k = kw.strip().strip('"').lower()
        plan.extend(("Google", tpl.format(kw=k)) for tpl in PLAIN_TEMPLATES[2:])
    return plan


def looks_like_job(title: str, snippet: str, url: str, platform: str) -> bool:
    text = f"{title} {snippet}"
    if platform == "LinkedIn" and "/jobs/view/" in url:
        return True
    if NOT_A_JOB.search(title):
        return False
    if platform == "Google":
        return bool(HIRING_TERMS.search(text) or JOB_BOARD_PATH.search(url))
    return bool(HIRING_TERMS.search(text))


class WebSearchSource(Source):
    name = "web_search"
    platforms = ("Google", "LinkedIn", "X", "Facebook", "Instagram")

    def __init__(self):
        cfg = get_config()
        self.provider = "serper" if cfg.serper_api_key else ("brave" if cfg.brave_api_key else None)

    def available(self) -> tuple[bool, str]:
        if not self.provider:
            return False, "Set SERPER_API_KEY or BRAVE_API_KEY"
        return True, ""

    def _search(self, query: str, fresh_days: int) -> list[dict]:
        cfg = get_config()
        if self.provider == "serper":
            data = post_json(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": cfg.serper_api_key, "Content-Type": "application/json"},
                json={"q": query, "num": 20, "tbs": "qdr:d" if fresh_days <= 1 else "qdr:w"},
            )
            return [
                {"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", ""),
                 "date": r.get("date")}
                for r in data.get("organic", [])
            ]
        data = get_json(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": cfg.brave_api_key, "Accept": "application/json"},
            params={"q": query, "count": 20, "freshness": "pd" if fresh_days <= 1 else "pw"},
        )
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", ""),
             "date": r.get("page_age") or r.get("age")}
            for r in (data.get("web") or {}).get("results", [])
        ]

    def _plan(self, ctx: FetchContext) -> list[tuple[str, str]]:
        cfg = get_config()
        if ctx.rotation.get(FREE_FLAG):
            return take_rotating(build_plain_queries(ctx), ctx, f"web_search:{self.provider}:plain",
                                 min(FREE_QUERIES_PER_RUN, cfg.max_search_queries_per_run))
        budget = ctx.query_budget.get("search", cfg.max_search_queries_per_run)
        return take_rotating(build_queries(ctx), ctx, f"web_search:{self.provider}", budget)

    def fetch(self, ctx: FetchContext) -> list[RawJob]:
        cfg = get_config()
        picked = self._plan(ctx)
        cost = cfg.serper_cost_per_query if self.provider == "serper" else cfg.brave_cost_per_query
        out: list[RawJob] = []
        i = 0
        while i < len(picked):
            platform, query = picked[i]
            i += 1
            try:
                results = self._search(query, 7 if ctx.weekly_sweep else 1)
            except Exception as exc:  # logged, never fatal for the run
                ctx.log_search(self.provider, platform, query, False, 0, str(exc))
                if self.provider == "serper" and FREE_PLAN_ERROR in str(exc) and not ctx.rotation.get(FREE_FLAG):
                    # Serper's free plan rejects site:, OR and other operators. Switch to plain queries from now on.
                    log.warning("Serper free plan detected: switching to plain search queries")
                    ctx.rotation[FREE_FLAG] = 1
                    picked, i = self._plan(ctx), 0
                    continue
                failures = getattr(self, "_failures", 0) + 1
                self._failures = failures
                if failures == 1:  # show the provider's reason once in the run log (GitHub Actions output)
                    log.warning("%s search failed: %s | query: %s", self.provider, exc, query[:200])
                continue
            ctx.record_usage(self.provider, 1, cost)
            kept = 0
            for r in results:
                url = r["url"]
                if not url:
                    continue
                actual = detect_platform(url) or "Google"
                if actual != platform and platform != "Google":
                    continue
                title = re.sub(r"\s*[|\-–]\s*(LinkedIn|Facebook|Instagram|X)\s*$", "", r["title"]).strip()
                snippet = r.get("snippet") or ""
                if not looks_like_job(title, snippet, url, actual):
                    continue
                posted = parse_date(r.get("date"), ctx.now)
                out.append(RawJob(
                    platform=actual, provider=self.provider, url=url, title=title or snippet[:120],
                    description=snippet, posted_at=posted, posted_text=r.get("date"),
                    platform_post_id=platform_post_id(url), query=query, is_snippet=True, raw=r,
                ))
                kept += 1
            ctx.log_search(self.provider, platform, query, True, kept, None)
        return out
