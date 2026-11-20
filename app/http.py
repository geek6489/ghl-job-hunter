"""Shared HTTP client with retries, backoff, per-host rate limiting and usage logging."""
from __future__ import annotations

import logging
import random
import threading
import time
from urllib.parse import urlsplit

import httpx

from .config import get_config

log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}

_host_lock = threading.Lock()
_host_last: dict[str, float] = {}
# Minimum seconds between requests to the same host (politeness / API rate limits).
HOST_MIN_INTERVAL = {
    "google.serper.dev": 0.2,
    "api.search.brave.com": 1.05,  # free plan: 1 query/second
    "serpapi.com": 0.5,
    "api.x.com": 1.0,
    "remotive.com": 2.0,
    "remoteok.com": 2.0,
    "himalayas.app": 1.0,
    "jobicy.com": 1.0,
    "weworkremotely.com": 1.0,
}
DEFAULT_MIN_INTERVAL = 1.0


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _throttle(url: str) -> None:
    host = urlsplit(url).hostname or ""
    gap = HOST_MIN_INTERVAL.get(host, DEFAULT_MIN_INTERVAL)
    with _host_lock:
        last = _host_last.get(host, 0.0)
        wait = last + gap - time.monotonic()
        _host_last[host] = max(time.monotonic(), last + gap)
    if wait > 0:
        time.sleep(wait)


_client: httpx.Client | None = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": get_config().user_agent, "Accept-Language": "en"},
        )
    return _client


def set_client(c: httpx.Client | None) -> None:
    """Tests inject an httpx.Client backed by MockTransport here."""
    global _client
    _client = c


def request(method: str, url: str, *, retries: int = 3, **kwargs) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        _throttle(url)
        try:
            resp = client().request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            last_exc = exc
            log.warning("HTTP %s %s failed (%s), attempt %d", method, url, exc, attempt + 1)
        else:
            if resp.status_code not in RETRY_STATUSES:
                return resp
            last_exc = HttpError(f"HTTP {resp.status_code} from {urlsplit(url).hostname}", resp.status_code)
            retry_after = resp.headers.get("retry-after")
            if retry_after and retry_after.isdigit() and attempt < retries:
                time.sleep(min(int(retry_after), 60))
                continue
        if attempt < retries:
            time.sleep(min(2 ** attempt + random.random(), 20))
    if isinstance(last_exc, HttpError):
        raise last_exc
    raise HttpError(f"{method} {urlsplit(url).hostname} failed: {last_exc}")


def get_json(url: str, **kwargs):
    resp = request("GET", url, **kwargs)
    if resp.status_code >= 400:
        raise HttpError(f"HTTP {resp.status_code} from {urlsplit(url).hostname}: {resp.text[:200]}", resp.status_code)
    return resp.json()


def post_json(url: str, **kwargs):
    resp = request("POST", url, **kwargs)
    if resp.status_code >= 400:
        raise HttpError(f"HTTP {resp.status_code} from {urlsplit(url).hostname}: {resp.text[:200]}", resp.status_code)
    return resp.json()
