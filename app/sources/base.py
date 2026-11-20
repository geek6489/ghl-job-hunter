from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable


@dataclass
class RawJob:
    """One result as a source returned it. Fields a source doesn't give stay None."""

    platform: str  # Google | LinkedIn | X | Facebook | Instagram | Job Boards
    provider: str  # serper | brave | serpapi_google_jobs | x_api | remotive | ...
    url: str
    title: str
    company: str | None = None
    description: str | None = None
    location: str | None = None
    posted_at: datetime | None = None
    posted_text: str | None = None
    salary_text: str | None = None
    employment_type: str | None = None
    work_arrangement: str | None = None
    deadline_at: datetime | None = None
    application_method: str | None = None
    recruiter_info: str | None = None
    platform_post_id: str | None = None
    query: str | None = None
    is_snippet: bool = False  # True when description is only a search-result snippet
    extra_urls: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class FetchContext:
    keywords: list[str]  # enabled keyword phrases, GHL group first
    ghl_keywords: list[str]
    automation_keywords: list[str]
    enabled_platforms: set[str]
    rotation: dict  # persisted rotation pointers, mutated by sources
    log_search: Callable[..., None]  # (provider, platform, query, ok, results, error)
    record_usage: Callable[..., None]  # (provider, units, cost_usd)
    query_budget: dict  # {"search": int, "google_jobs": int}
    last_run: dict  # provider -> datetime of last successful fetch (for slow-poll providers)
    now: datetime | None = None
    weekly_sweep: bool = False  # True once a day: look back a week for still-open older jobs


class Source:
    name: str = "base"
    platforms: tuple[str, ...] = ()
    # Minimum minutes between fetches (free APIs ask clients to poll gently)
    min_interval_minutes: int = 0

    def available(self) -> tuple[bool, str]:
        return True, ""

    def fetch(self, ctx: FetchContext) -> list[RawJob]:  # pragma: no cover - interface
        raise NotImplementedError


def chunk_or_groups(phrases: list[str], size: int = 4) -> list[str]:
    groups = []
    for i in range(0, len(phrases), size):
        part = phrases[i : i + size]
        groups.append("(" + " OR ".join(f'"{p}"' for p in part) + ")")
    return groups


def take_rotating(items: list, ctx: FetchContext, key: str, n: int) -> list:
    """Return the next n items, continuing where the previous run stopped."""
    if not items or n <= 0:
        return []
    start = int(ctx.rotation.get(key, 0)) % len(items)
    n = min(n, len(items))
    picked = [items[(start + i) % len(items)] for i in range(n)]
    ctx.rotation[key] = (start + n) % len(items)
    return picked
