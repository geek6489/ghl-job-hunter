from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.models import Job, Keyword
from app.pipeline import run_monitor
from app.sources import RemotiveSource, WebSearchSource


def client():
    from app.web.main import app
    return TestClient(app)


def login(c):
    r = c.post("/login", data={"username": "admin", "password": "correct horse", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303


def test_requires_login(env):
    with client() as c:
        r = c.get("/jobs", follow_redirects=False)
        assert r.status_code == 303 and "/login" in r.headers["location"]
        assert c.post("/login", data={"username": "admin", "password": "nope"}).status_code == 200
        assert c.get("/api/jobs").status_code == 401
        assert c.get("/healthz").json()["web"] == "ok"


def test_pages_filters_tracker_and_settings(net):
    with session_scope() as s:
        run_monitor(s, trigger="manual", sources=[WebSearchSource(), RemotiveSource()])
    with client() as c:
        login(c)
        for path in ["/", "/jobs", "/tracker", "/summaries", "/settings", "/health",
                     "/jobs?platform=LinkedIn&min_score=60&arrangement=Remote&posted_within=1&sort=newest&q=ghl"]:
            r = c.get(path)
            assert r.status_code == 200, (path, r.text[:500])
        assert "Partial" in c.get("/health").text  # coverage table is honest about LinkedIn etc.
        r = c.get("/jobs?platform=Job+Boards")
        assert "CRM Automation Specialist" in r.text and "GoHighLevel Automation Specialist" not in r.text
        with session_scope() as s:
            job = s.scalars(select(Job)).first()
            jid = job.id
        assert job.primary_url in c.get(f"/jobs/{jid}").text
        r = c.post(f"/jobs/{jid}/tracker", data={"tracker_status": "Applied", "notes": "sent loom", "applied_at": "", "status": "Active"})
        assert r.status_code == 200
        with session_scope() as s:
            j = s.get(Job, jid)
            assert j.tracker_status == "Applied" and j.applied_at and j.notes == "sent loom"
        assert "sent loom" in c.get("/tracker").text
        c.post("/settings/keywords", data={"text": "HighLevel SaaS Mode", "group": "ghl"})
        with session_scope() as s:
            assert s.scalar(select(Keyword).where(Keyword.text == "HighLevel SaaS Mode"))
        c.post("/settings", data={"platform_Google": "on", "monitor_interval_minutes": "180",
                                  "notification_frequency": "daily", "min_relevance_score": "70", "profile": "me"})
        from app import settings_store
        with session_scope() as s:
            assert settings_store.get(s, "monitor_interval_minutes") == 180
            assert settings_store.get(s, "platforms")["LinkedIn"] is False
        c.post("/summaries/generate", data={})
        assert "New" in c.get("/summaries").text
        assert c.get("/api/jobs").status_code == 200
        # cross-site POSTs are refused
        assert c.post("/run-now", headers={"origin": "https://evil.example"}).status_code == 403


def test_mark_applied_moves_job_to_applied_screen(net):
    with session_scope() as s:
        run_monitor(s, trigger="manual", sources=[WebSearchSource(), RemotiveSource()])
        jid = s.scalars(select(Job).where(Job.is_job_posting.is_(True))).first().id
    with client() as c:
        login(c)
        assert 'data-act="apply" data-id="%s"' % jid in c.get("/").text
        r = c.post(f"/jobs/{jid}/applied", data={"applied": "1"})
        assert r.json()["tracker_status"] == "Applied"
        page = c.get("/").text
        assert 'class="jc st-applied" data-id="%s"' % jid in page
        c.post(f"/jobs/{jid}/applied", data={"applied": "0"})
        with session_scope() as s:
            j = s.get(Job, jid)
            assert j.tracker_status == "New" and j.applied_at is None


def test_health_shows_worker_keys_not_dashboard_keys():
    from app import settings_store
    with session_scope() as s:
        settings_store.put(s, "worker_config", {"serper_api_key": True, "resend_api_key": True, "email_from": True,
                                                "email_provider": "resend"})
    with client() as c:
        login(c)
        page = c.get("/health").text
        assert "Web search API (Serper)" in page
        assert 'class="y">Email (resend)' in page


def test_not_relevant_moves_job_to_its_own_screen(net):
    with session_scope() as s:
        run_monitor(s, trigger="manual", sources=[WebSearchSource(), RemotiveSource()])
        jid = s.scalars(select(Job).where(Job.is_job_posting.is_(True))).first().id
    with client() as c:
        login(c)
        assert 'class="jc st-open" data-id="%s"' % jid in c.get("/").text
        assert c.post(f"/jobs/{jid}/hide", data={"hidden": "1"}).json()["tracker_status"] == "Not Interested"
        page = c.get("/").text
        assert 'class="jc st-hidden" data-id="%s"' % jid in page
        assert 'class="jc st-open" data-id="%s"' % jid not in page
        c.post(f"/jobs/{jid}/hide", data={"hidden": "0"})
        assert 'class="jc st-open" data-id="%s"' % jid in c.get("/").text
