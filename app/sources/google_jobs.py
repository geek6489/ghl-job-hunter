"""Google Jobs results through SerpApi (structured listings from career sites,
LinkedIn, Indeed, Glassdoor and other boards that Google Jobs aggregates)."""
from __future__ import annotations

from ..config import get_config
from ..http import get_json
from ..normalize import detect_platform, parse_date, platform_post_id
from .base import FetchContext, RawJob, Source, take_rotating

PER_RUN = 2  # SerpApi bills per search, so keywords rotate across runs


class GoogleJobsSource(Source):
    name = "serpapi_google_jobs"
    platforms = ("Google",)

    def available(self) -> tuple[bool, str]:
        if not get_config().serpapi_api_key:
            return False, "Set SERPAPI_API_KEY for structured Google Jobs listings"
        return True, ""

    def fetch(self, ctx: FetchContext) -> list[RawJob]:
        if "Google" not in ctx.enabled_platforms:
            return []
        cfg = get_config()
        budget = ctx.query_budget.get("google_jobs", PER_RUN)
        out: list[RawJob] = []
        for kw in take_rotating(ctx.keywords, ctx, self.name, budget):
            params = {"engine": "google_jobs", "q": kw, "api_key": cfg.serpapi_api_key, "hl": "en"}
            try:
                data = get_json("https://serpapi.com/search.json", params=params)
            except Exception as exc:
                ctx.log_search(self.name, "Google", kw, False, 0, str(exc))
                continue
            ctx.record_usage(self.name, 1, cfg.serpapi_cost_per_query)
            if data.get("error") and "hasn't returned any results" not in data["error"]:
                ctx.log_search(self.name, "Google", kw, False, 0, data["error"])
                continue
            results = data.get("jobs_results") or []
            for j in results:
                out.append(self._to_raw(j, kw, ctx))
            ctx.log_search(self.name, "Google", kw, True, len(results), None)
        return out

    @staticmethod
    def _to_raw(j: dict, query: str, ctx: FetchContext) -> RawJob:
        ext = j.get("detected_extensions") or {}
        apply_opts = j.get("apply_options") or []
        urls = [a.get("link") for a in apply_opts if a.get("link")]
        url = urls[0] if urls else (j.get("share_link") or "")
        highlights = []
        for h in j.get("job_highlights") or []:
            items = h.get("items") or []
            if items:
                highlights.append(f"{h.get('title', '')}:\n- " + "\n- ".join(items))
        description = (j.get("description") or "")
        if highlights:
            description += "\n\n" + "\n\n".join(highlights)
        arrangement = "Remote" if ext.get("work_from_home") else None
        return RawJob(
            platform=detect_platform(url) or "Google",
            provider="serpapi_google_jobs",
            url=url,
            title=j.get("title") or "",
            company=j.get("company_name"),
            description=description,
            location=j.get("location"),
            posted_at=parse_date(ext.get("posted_at"), ctx.now),
            posted_text=ext.get("posted_at"),
            salary_text=ext.get("salary"),
            employment_type=ext.get("schedule_type"),
            work_arrangement=arrangement,
            application_method=("Apply via " + ", ".join(a.get("title", "") for a in apply_opts[:4])) if apply_opts else None,
            platform_post_id=("gj:" + j["job_id"][:150]) if j.get("job_id") else platform_post_id(url),
            query=query,
            extra_urls=urls[1:6],
            raw={"via": j.get("via"), "job_id": j.get("job_id")},
        )
