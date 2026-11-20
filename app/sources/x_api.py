"""Official X API v2 recent search (last 7 days). Needs a paid X developer plan."""
from __future__ import annotations

from ..config import get_config
from ..http import get_json
from ..normalize import parse_date
from .base import FetchContext, RawJob, Source, take_rotating

BASE_TERMS = ['"gohighlevel"', '"go high level"', '"highlevel"', "GHL", '"marketing automation"',
              '"crm automation"', '"workflow automation"', '"automation specialist"']


class XApiSource(Source):
    name = "x_api"
    platforms = ("X",)
    min_interval_minutes = 60

    def available(self) -> tuple[bool, str]:
        if not get_config().x_bearer_token:
            return False, "Set X_BEARER_TOKEN (paid X API access) for direct X search"
        return True, ""

    def fetch(self, ctx: FetchContext) -> list[RawJob]:
        if "X" not in ctx.enabled_platforms:
            return []
        cfg = get_config()
        groups = [" OR ".join(BASE_TERMS[:4]), " OR ".join(BASE_TERMS[4:])]
        out: list[RawJob] = []
        for g in take_rotating(groups, ctx, self.name, 1):
            query = f"({g}) (hiring OR \"looking for\" OR \"we're hiring\" OR freelance OR contract) -is:retweet lang:en"
            params = {
                "query": query, "max_results": 50, "tweet.fields": "created_at,author_id,entities",
                "expansions": "author_id", "user.fields": "username,name,description",
            }
            try:
                data = get_json(
                    "https://api.x.com/2/tweets/search/recent",
                    params=params, headers={"Authorization": f"Bearer {cfg.x_bearer_token}"},
                )
            except Exception as exc:
                ctx.log_search(self.name, "X", query, False, 0, str(exc))
                continue
            ctx.record_usage(self.name, 1, cfg.x_cost_per_request)
            users = {u["id"]: u for u in (data.get("includes") or {}).get("users", [])}
            tweets = data.get("data") or []
            for t in tweets:
                u = users.get(t.get("author_id"), {})
                username = u.get("username") or "i"
                text = t.get("text", "")
                out.append(RawJob(
                    platform="X", provider=self.name,
                    url=f"https://x.com/{username}/status/{t['id']}",
                    title=text.split("\n")[0][:140],
                    description=text,
                    posted_at=parse_date(t.get("created_at")),
                    posted_text=t.get("created_at"),
                    recruiter_info=f"@{username} ({u.get('name')})" if u else None,
                    application_method="Reply or DM on X" if u else None,
                    platform_post_id=f"x:{t['id']}", query=query, raw={"tweet": t},
                ))
            ctx.log_search(self.name, "X", query, True, len(tweets), None)
        return out
