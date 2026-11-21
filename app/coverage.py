"""Honest, config-aware description of what each platform integration can actually see."""
from __future__ import annotations

from .config import get_config

FULL, PARTIAL, NEEDS_AUTH, OFF = "Fully integrated", "Partial", "Needs authorization", "Not configured"


def platform_coverage(cfg=None) -> list[dict]:
    cfg = cfg or get_config()
    search = bool(cfg.serper_api_key or cfg.brave_api_key)
    search_name = "Serper (Google results)" if cfg.serper_api_key else ("Brave Search API" if cfg.brave_api_key else "a search API")
    rows = [
        {
            "platform": "Google",
            "level": FULL if (cfg.serpapi_api_key or search) else OFF,
            "how": " + ".join(filter(None, [
                "Google Jobs via SerpApi (structured listings)" if cfg.serpapi_api_key else None,
                f"Web search via {search_name} for career pages and job boards" if search else None,
            ])) or "Add SERPAPI_API_KEY and/or SERPER_API_KEY",
            "limits": "Only pages Google has indexed. Google Jobs also aggregates many LinkedIn, Indeed and company listings.",
        },
        {
            "platform": "LinkedIn",
            "level": PARTIAL if search or cfg.serpapi_api_key else OFF,
            "how": f"Public job pages and posts that {search_name} has indexed (site:linkedin.com), plus LinkedIn listings surfaced in Google Jobs.",
            "limits": "LinkedIn has no public job-search API and forbids scraping. Full recruiter-post coverage is not possible; partner Job Posting APIs are only for ATS vendors.",
        },
        {
            "platform": "X",
            "level": FULL if cfg.x_bearer_token else (PARTIAL if search else NEEDS_AUTH),
            "how": "Official X API v2 recent search" if cfg.x_bearer_token else f"Public posts indexed by {search_name} (site:x.com).",
            "limits": "Direct search needs a paid X API plan (X_BEARER_TOKEN). Search-engine coverage of X is thin and delayed.",
        },
        {
            "platform": "Facebook",
            "level": PARTIAL if search else NEEDS_AUTH,
            "how": f"Public pages and public group posts indexed by {search_name} (site:facebook.com).",
            "limits": "Meta removed the Groups API in 2024 and offers no public post search. Private groups can't be monitored by any permitted method.",
        },
        {
            "platform": "Instagram",
            "level": PARTIAL if search else NEEDS_AUTH,
            "how": f"Public posts indexed by {search_name} (site:instagram.com).",
            "limits": "The Graph API hashtag search needs a Business account plus Meta app review and is capped at 30 hashtags per 7 days. Not built yet.",
        },
        {
            "platform": "Job Boards",
            "level": FULL,
            "how": "Remotive, Remote OK, Himalayas, Jobicy and We Work Remotely public APIs/RSS (no key needed).",
            "limits": "Remote roles only. Polled every 2 to 6 hours as these boards request.",
        },
    ]
    return rows
