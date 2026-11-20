"""Free, public job-board APIs and RSS feeds that allow programmatic access.
Each one links back to the original listing, which their terms ask for."""
from __future__ import annotations

import feedparser

from ..http import get_json, request
from ..normalize import is_relevant, parse_date, strip_html
from .base import FetchContext, RawJob, Source

SEARCH_TERMS = ["gohighlevel", "highlevel", "marketing automation", "crm", "automation", "zapier"]


def _relevant(*parts: str | None) -> bool:
    return is_relevant(" ".join(p or "" for p in parts))


class RemotiveSource(Source):
    name = "remotive"
    platforms = ("Job Boards",)
    min_interval_minutes = 360  # Remotive asks API users to poll a few times a day at most

    def fetch(self, ctx: FetchContext) -> list[RawJob]:
        out: list[RawJob] = []
        seen: set[str] = set()
        for term in ["gohighlevel", "automation", "crm"]:
            url = "https://remotive.com/api/remote-jobs"
            try:
                data = get_json(url, params={"search": term, "limit": 100})
            except Exception as exc:
                ctx.log_search(self.name, "Job Boards", term, False, 0, str(exc))
                continue
            jobs = data.get("jobs", [])
            kept = 0
            for j in jobs:
                link = j.get("url")
                if not link or link in seen:
                    continue
                seen.add(link)
                desc = strip_html(j.get("description"))
                if not _relevant(j.get("title"), desc):
                    continue
                kept += 1
                out.append(RawJob(
                    platform="Job Boards", provider=self.name, url=link, title=j.get("title", ""),
                    company=j.get("company_name"), description=desc,
                    location=j.get("candidate_required_location"), work_arrangement="Remote",
                    posted_at=parse_date(j.get("publication_date")), posted_text=j.get("publication_date"),
                    salary_text=j.get("salary") or None, employment_type=(j.get("job_type") or "").replace("_", " ").title() or None,
                    application_method="Apply via Remotive listing", platform_post_id=f"remotive:{j.get('id')}",
                    query=term, raw={"id": j.get("id"), "board": "Remotive"},
                ))
            ctx.log_search(self.name, "Job Boards", term, True, kept, None)
        return out


class RemoteOKSource(Source):
    name = "remoteok"
    platforms = ("Job Boards",)
    min_interval_minutes = 360

    def fetch(self, ctx: FetchContext) -> list[RawJob]:
        try:
            data = get_json("https://remoteok.com/api")
        except Exception as exc:
            ctx.log_search(self.name, "Job Boards", "feed", False, 0, str(exc))
            return []
        out: list[RawJob] = []
        for j in data:
            if not isinstance(j, dict) or not j.get("id") or not j.get("position"):
                continue  # first element is the legal notice
            desc = strip_html(j.get("description"))
            tags = " ".join(j.get("tags") or [])
            if not _relevant(j.get("position"), desc, tags):
                continue
            salary = None
            if j.get("salary_min") and j.get("salary_max"):
                salary = f"USD {int(j['salary_min']):,} - {int(j['salary_max']):,} per year"
            out.append(RawJob(
                platform="Job Boards", provider=self.name, url=j.get("url") or f"https://remoteok.com/remote-jobs/{j['id']}",
                title=j["position"], company=j.get("company"), description=desc, location=j.get("location") or None,
                work_arrangement="Remote", posted_at=parse_date(j.get("epoch") or j.get("date")),
                posted_text=j.get("date"), salary_text=salary, application_method="Apply via Remote OK listing",
                platform_post_id=f"remoteok:{j['id']}", raw={"board": "Remote OK"},
            ))
        ctx.log_search(self.name, "Job Boards", "feed", True, len(out), None)
        return out


class HimalayasSource(Source):
    name = "himalayas"
    platforms = ("Job Boards",)
    min_interval_minutes = 180

    def fetch(self, ctx: FetchContext) -> list[RawJob]:
        out: list[RawJob] = []
        for term in ["gohighlevel", "marketing automation", "crm automation"]:
            try:
                data = get_json("https://himalayas.app/jobs/api/search", params={"q": term, "sort": "recent"})
            except Exception as exc:
                ctx.log_search(self.name, "Job Boards", term, False, 0, str(exc))
                continue
            jobs = data.get("jobs", [])
            kept = 0
            for j in jobs:
                desc = strip_html(j.get("description") or j.get("excerpt"))
                if not _relevant(j.get("title"), desc):
                    continue
                kept += 1
                salary = None
                if j.get("minSalary") and j.get("maxSalary"):
                    salary = f"{j.get('currency') or 'USD'} {int(j['minSalary']):,} - {int(j['maxSalary']):,}"
                locs = j.get("locationRestrictions") or []
                out.append(RawJob(
                    platform="Job Boards", provider=self.name,
                    url=j.get("applicationLink") or j.get("guid") or "", title=j.get("title", ""),
                    company=j.get("companyName"), description=desc,
                    location=", ".join(locs) if locs else None, work_arrangement="Remote",
                    posted_at=parse_date(j.get("pubDate")), deadline_at=parse_date(j.get("expiryDate")),
                    salary_text=salary, employment_type=j.get("employmentType"),
                    platform_post_id=f"himalayas:{j.get('guid')}" if j.get("guid") else None,
                    query=term, extra_urls=[j["guid"]] if j.get("guid") and j.get("applicationLink") else [],
                    raw={"board": "Himalayas"},
                ))
            ctx.log_search(self.name, "Job Boards", term, True, kept, None)
        return [r for r in out if r.url]


class JobicySource(Source):
    name = "jobicy"
    platforms = ("Job Boards",)
    min_interval_minutes = 360

    def fetch(self, ctx: FetchContext) -> list[RawJob]:
        out: list[RawJob] = []
        for tag in ["automation", "crm", "marketing automation"]:
            try:
                data = get_json("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": tag})
            except Exception as exc:
                ctx.log_search(self.name, "Job Boards", tag, False, 0, str(exc))
                continue
            jobs = data.get("jobs", [])
            kept = 0
            for j in jobs:
                desc = strip_html(j.get("jobDescription") or j.get("jobExcerpt"))
                if not _relevant(j.get("jobTitle"), desc):
                    continue
                kept += 1
                salary = None
                if j.get("annualSalaryMin") and j.get("annualSalaryMax"):
                    salary = f"{j.get('salaryCurrency') or 'USD'} {int(float(j['annualSalaryMin'])):,} - {int(float(j['annualSalaryMax'])):,} per year"
                jt = j.get("jobType")
                out.append(RawJob(
                    platform="Job Boards", provider=self.name, url=j.get("url", ""), title=j.get("jobTitle", ""),
                    company=j.get("companyName"), description=desc, location=j.get("jobGeo"),
                    work_arrangement="Remote", posted_at=parse_date(j.get("pubDate")), posted_text=j.get("pubDate"),
                    salary_text=salary, employment_type=", ".join(jt) if isinstance(jt, list) else jt,
                    platform_post_id=f"jobicy:{j.get('id')}", query=tag, raw={"board": "Jobicy"},
                ))
            ctx.log_search(self.name, "Job Boards", tag, True, kept, None)
        return [r for r in out if r.url]


class WeWorkRemotelySource(Source):
    name = "weworkremotely"
    platforms = ("Job Boards",)
    min_interval_minutes = 120
    FEEDS = [
        "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
        "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
        "https://weworkremotely.com/categories/all-other-remote-jobs.rss",
        "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    ]

    def fetch(self, ctx: FetchContext) -> list[RawJob]:
        out: list[RawJob] = []
        for feed_url in self.FEEDS:
            try:
                resp = request("GET", feed_url)
                feed = feedparser.parse(resp.content)
            except Exception as exc:
                ctx.log_search(self.name, "Job Boards", feed_url, False, 0, str(exc))
                continue
            kept = 0
            for e in feed.entries:
                desc = strip_html(e.get("summary") or e.get("description"))
                title = e.get("title", "")
                if not _relevant(title, desc):
                    continue
                company, _, role = title.partition(":")
                kept += 1
                out.append(RawJob(
                    platform="Job Boards", provider=self.name, url=e.get("link", ""),
                    title=(role or title).strip(), company=company.strip() if role else None, description=desc,
                    location=e.get("region"), work_arrangement="Remote",
                    posted_at=parse_date(e.get("published")), posted_text=e.get("published"),
                    platform_post_id=f"wwr:{e.get('id') or e.get('link')}"[:200], raw={"board": "We Work Remotely"},
                ))
            ctx.log_search(self.name, "Job Boards", feed_url, True, kept, None)
        return [r for r in out if r.url]
