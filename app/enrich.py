"""Fill in details for search results by reading the public job page itself.

Only non-social pages are fetched (career sites, ATS pages, job boards), robots.txt is
respected, and the structured schema.org JobPosting data that sites publish for Google
is preferred over guessing from page text."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .config import get_config
from .http import request
from .normalize import SOCIAL_PLATFORMS, detect_platform, parse_date, strip_html

log = logging.getLogger(__name__)
_robots: dict[str, RobotFileParser | None] = {}

# Aggregators whose terms disallow automated fetching; we keep their search snippet only.
NO_FETCH_HOSTS = ("indeed.", "glassdoor.", "ziprecruiter.", "upwork.com", "fiverr.com", "google.com")


@dataclass
class PageInfo:
    status: int | None = None
    closed: bool = False
    title: str | None = None
    company: str | None = None
    description: str | None = None
    location: str | None = None
    posted_at: object = None
    deadline_at: object = None
    salary_text: str | None = None
    employment_type: str | None = None
    work_arrangement: str | None = None
    apply_url: str | None = None


def can_fetch(url: str) -> bool:
    if detect_platform(url) in SOCIAL_PLATFORMS:
        return False
    host = (urlsplit(url).hostname or "").lower()
    if not host or any(h in host for h in NO_FETCH_HOSTS):
        return False
    base = f"{urlsplit(url).scheme}://{host}"
    if base not in _robots:
        rp = RobotFileParser()
        try:
            resp = request("GET", base + "/robots.txt", retries=1)
            if resp.status_code >= 400:
                rp = None  # no robots.txt: allowed
            else:
                rp.parse(resp.text.splitlines())
        except Exception:
            rp = None
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(get_config().user_agent, url)


CLOSED_PATTERNS = re.compile(
    r"(no longer accepting applications|this job (?:is|has) (?:expired|closed)|position (?:has been )?filled|"
    r"job (?:posting )?(?:is )?no longer available|this position is closed|applications? (?:are )?closed)",
    re.I,
)

_EMPLOYMENT_MAP = {
    "FULL_TIME": "Full-time", "PART_TIME": "Part-time", "CONTRACTOR": "Contract", "TEMPORARY": "Contract",
    "INTERN": "Internship", "PER_DIEM": "Contract", "OTHER": None, "VOLUNTEER": None,
}


def _find_jobposting(obj):
    if isinstance(obj, list):
        for o in obj:
            r = _find_jobposting(o)
            if r:
                return r
    elif isinstance(obj, dict):
        t = obj.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            return obj
        if "@graph" in obj:
            return _find_jobposting(obj["@graph"])
    return None


def _salary(bs) -> str | None:
    if not isinstance(bs, dict):
        return None
    cur = bs.get("currency") or ""
    v = bs.get("value")
    if isinstance(v, dict):
        lo, hi, unit = v.get("minValue"), v.get("maxValue"), v.get("unitText")
        val = v.get("value")
        if lo and hi:
            amount = f"{lo:,} - {hi:,}" if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) else f"{lo} - {hi}"
        elif val or lo or hi:
            amount = str(val or lo or hi)
        else:
            return None
        return f"{cur} {amount}{(' per ' + unit.lower()) if unit else ''}".strip()
    if v:
        return f"{cur} {v}".strip()
    return None


def parse_jobposting_html(html_text: str) -> PageInfo:
    info = PageInfo()
    for m in re.finditer(r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html_text):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        jp = _find_jobposting(data)
        if not jp:
            continue
        info.title = strip_html(jp.get("title")) or None
        org = jp.get("hiringOrganization")
        info.company = (org.get("name") if isinstance(org, dict) else org) or None
        info.description = strip_html(jp.get("description")) or None
        info.posted_at = parse_date(jp.get("datePosted"))
        info.deadline_at = parse_date(jp.get("validThrough")) if jp.get("validThrough") else None
        if jp.get("validThrough") and info.deadline_at is None:
            # parse_date rejects future dates for "posted" values; deadlines are allowed to be future
            from dateutil import parser as dp
            try:
                d = dp.parse(str(jp["validThrough"]))
                info.deadline_at = d.replace(tzinfo=None)
            except (ValueError, OverflowError):
                pass
        info.salary_text = _salary(jp.get("baseSalary"))
        et = jp.get("employmentType")
        if isinstance(et, list):
            et = et[0] if et else None
        if et:
            info.employment_type = _EMPLOYMENT_MAP.get(str(et).upper(), str(et).replace("_", " ").title())
        if str(jp.get("jobLocationType", "")).upper() == "TELECOMMUTE":
            info.work_arrangement = "Remote"
        loc = jp.get("jobLocation")
        if isinstance(loc, list):
            loc = loc[0] if loc else None
        if isinstance(loc, dict):
            addr = loc.get("address") or {}
            if isinstance(addr, dict):
                parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
                parts = [p.get("name") if isinstance(p, dict) else p for p in parts]
                info.location = ", ".join(str(p) for p in parts if p) or None
        if not info.location and jp.get("applicantLocationRequirements"):
            alr = jp["applicantLocationRequirements"]
            alr = alr if isinstance(alr, list) else [alr]
            info.location = ", ".join(a.get("name", "") for a in alr if isinstance(a, dict)) or None
        info.apply_url = jp.get("url") or None
        break
    if not info.description:
        body = strip_html(html_text)
        info.description = body[:8000] if body else None
    if CLOSED_PATTERNS.search(html_text[:200000]):
        info.closed = True
    return info


def fetch_page_info(url: str) -> PageInfo | None:
    if not can_fetch(url):
        return None
    try:
        resp = request("GET", url, retries=1, headers={"Accept": "text/html"})
    except Exception as exc:
        log.info("enrich fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code in (404, 410):
        return PageInfo(status=resp.status_code, closed=True)
    if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", "html"):
        return None
    info = parse_jobposting_html(resp.text)
    info.status = resp.status_code
    return info
