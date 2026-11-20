import json
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh SQLite database and config for each test."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper")
    monkeypatch.setenv("RESEND_API_KEY", "test-resend")
    monkeypatch.setenv("EMAIL_FROM", "Jobs <jobs@example.com>")
    monkeypatch.setenv("EMAIL_TO", "me@example.com")
    for k in ["ANTHROPIC_API_KEY", "SERPAPI_API_KEY", "X_BEARER_TOKEN", "BRAVE_API_KEY", "HEALTHCHECK_PING_URL"]:
        monkeypatch.delenv(k, raising=False)
    from app import config, db, http

    config.reset_config()
    db.reset_engine()
    monkeypatch.setattr(http, "_throttle", lambda url: None)
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    db.init_db()
    from app import settings_store

    with db.session_scope() as s:
        settings_store.ensure_defaults(s)
    yield
    db.reset_engine()
    http.set_client(None)


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FakeNet:
    """Routes requests to recorded-shape responses. Unknown hosts get 404 so nothing leaks out."""

    def __init__(self):
        self.calls = []
        self.emails = []
        self.serper = load("serper_search.json")
        self.pages = {}

    def handler(self, request: httpx.Request):
        self.calls.append(request)
        host, path = request.url.host, request.url.path
        if host == "google.serper.dev":
            return httpx.Response(200, json=self.serper)
        if host == "api.resend.com":
            self.emails.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "email_1"})
        if host == "remotive.com":
            return httpx.Response(200, json=load("remotive.json"))
        if host == "remoteok.com":
            return httpx.Response(200, json=load("remoteok.json"))
        if path == "/robots.txt":
            return httpx.Response(404)
        url = str(request.url)
        if url in self.pages:
            return httpx.Response(200, text=self.pages[url], headers={"content-type": "text/html"})
        return httpx.Response(404, json={"error": "not mocked"})


@pytest.fixture()
def net(env):
    from app import http

    fake = FakeNet()
    http.set_client(httpx.Client(transport=httpx.MockTransport(fake.handler)))
    return fake
