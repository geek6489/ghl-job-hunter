"""URL canonicalisation, text normalisation, platform detection and date parsing."""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dateutil import parser as dateparser

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id", "gclid", "fbclid",
    "mc_cid", "mc_eid", "ref", "refid", "trk", "trackingid", "src", "source", "s", "t", "igshid",
    "lipi", "originalsubdomain", "si", "rcm", "currentjobid", "eBP", "refId", "trackingId",
}

SOCIAL_PLATFORMS = {"LinkedIn", "X", "Facebook", "Instagram"}


def detect_platform(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith("linkedin.com") or host == "lnkd.in":
        return "LinkedIn"
    if host in {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"}:
        return "X"
    if host.endswith("facebook.com") or host in {"fb.com", "fb.me", "m.facebook.com"}:
        return "Facebook"
    if host.endswith("instagram.com"):
        return "Instagram"
    return None


def canonical_url(url: str) -> str:
    """Lower-case host, drop fragments and tracking params, normalise social URL shapes."""
    url = url.strip()
    parts = urlsplit(url)
    scheme = "https"
    host = (parts.hostname or "").lower()
    if host.startswith("www.") or host.startswith("m.") or host.startswith("mobile."):
        host = host.split(".", 1)[1]
    if host == "twitter.com":
        host = "x.com"
    # LinkedIn regional subdomains: pk.linkedin.com -> linkedin.com
    if host.endswith(".linkedin.com"):
        host = "linkedin.com"
    path = re.sub(r"/+$", "", parts.path) or "/"
    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in {p.lower() for p in TRACKING_PARAMS}
    ]
    # LinkedIn job views: /jobs/view/<slug>-<id> -> /jobs/view/<id>
    m = re.match(r"^/jobs/view/(?:.*-)?(\d{6,})$", path)
    if host == "linkedin.com" and m:
        path = f"/jobs/view/{m.group(1)}"
        query_pairs = []
    # X statuses: /<user>/status/<id> -> /i/status/<id>
    m = re.match(r"^/[^/]+/status(?:es)?/(\d+)", path)
    if host == "x.com" and m:
        path = f"/i/status/{m.group(1)}"
        query_pairs = []
    query = urlencode(sorted(query_pairs))
    return urlunsplit((scheme, host, path, query, ""))


def platform_post_id(url: str) -> str | None:
    c = canonical_url(url)
    patterns = [
        (r"linkedin\.com/jobs/view/(\d+)", "li-job"),
        (r"linkedin\.com/(?:posts|feed/update)/.*?(\d{15,})", "li-post"),
        (r"x\.com/i/status/(\d+)", "x"),
        (r"facebook\.com/groups/[^/]+/(?:posts|permalink)/(\d+)", "fb-group"),
        (r"facebook\.com/.*/posts/([\w]+)", "fb-post"),
        (r"instagram\.com/(?:p|reel)/([\w-]+)", "ig"),
    ]
    for pattern, prefix in patterns:
        m = re.search(pattern, c)
        if m:
            return f"{prefix}:{m.group(1)}"
    return None


_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|co|company|gmbh|pvt|plc|llp|agency|group|the)\b\.?", re.I
)


def norm_text(s: str | None) -> str:
    s = html.unescape(s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_title(title: str | None) -> str:
    t = norm_text(title)
    t = re.sub(r"\b(remote|hybrid|onsite|on site|urgent|hiring|full time|part time|contract|freelance)\b", " ", t)
    t = t.replace("go high level", "gohighlevel").replace("highlevel", "gohighlevel")
    t = re.sub(r"\bghl\b", "gohighlevel", t)
    t = t.replace("gogohighlevel", "gohighlevel")
    return re.sub(r"\s+", " ", t).strip()


def norm_company(company: str | None) -> str:
    if not company or company.strip().lower() in {"not specified", "unknown", "n/a", ""}:
        return ""
    c = _COMPANY_SUFFIXES.sub(" ", norm_text(company))
    return re.sub(r"\s+", " ", c).strip()


def content_hash(*parts: str | None) -> str:
    joined = "\n".join(norm_text(p) for p in parts)
    return hashlib.sha256(joined.encode()).hexdigest()


def strip_html(s: str | None) -> str:
    s = s or ""
    s = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


_REL_RE = re.compile(r"(\d+)\s*(minute|min|hour|hr|day|week|month)s?\s*ago", re.I)


def parse_date(value, now: datetime | None = None) -> datetime | None:
    """Parse ISO dates, unix timestamps and relative strings such as '3 hours ago'.
    Returns naive UTC, or None when the value can't be read (never guesses)."""
    if value is None or value == "":
        return None
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip()
    low = text.lower()
    if low in {"just now", "today", "just posted"}:
        return now
    if low == "yesterday":
        return now - timedelta(days=1)
    m = _REL_RE.search(low)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {
            "minute": timedelta(minutes=n), "min": timedelta(minutes=n), "hour": timedelta(hours=n),
            "hr": timedelta(hours=n), "day": timedelta(days=n), "week": timedelta(weeks=n),
            "month": timedelta(days=30 * n),
        }[unit]
        return now - delta
    if text.isdigit() and len(text) >= 9:
        return parse_date(int(text), now)
    try:
        dt = dateparser.parse(text, fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    if dt > now + timedelta(days=1):
        # dateutil fills missing parts from today; a future "posted" date is not trustworthy
        return None
    return dt


GHL_TERMS = re.compile(r"\b(go\s?high\s?level|gohighlevel|highlevel|high level crm|ghl|lead\s?connector)\b", re.I)
RELEVANT_TERMS = re.compile(
    r"\b(go\s?high\s?level|gohighlevel|highlevel|ghl|lead\s?connector|crm|automation|automations|automate|"
    r"workflow|workflows|zapier|make\.com|integromat|pabbly|n8n|hubspot|keap|infusionsoft|activecampaign|"
    r"clickfunnels|funnel|funnels|marketing operations|marketing ops|revops|salesforce|pipedrive|"
    r"zoho|api integration|integrations?)\b",
    re.I,
)
HIRING_TERMS = re.compile(
    r"\b(hiring|we.?re hiring|looking for|seeking|job|jobs|position|role|opening|vacancy|apply|"
    r"contract|freelancer|freelance|recruit|join our team|need an?|need a)\b",
    re.I,
)


def is_relevant(text: str) -> bool:
    return bool(RELEVANT_TERMS.search(text or ""))


def mentions_ghl(text: str) -> bool:
    return bool(GHL_TERMS.search(text or ""))
