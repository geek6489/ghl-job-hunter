from sqlalchemy import func, select

from app import settings_store
from app.db import session_scope
from app.models import Job, JobSource, Notification, SearchLog
from app.pipeline import run_monitor
from app.sources import RemoteOKSource, RemotiveSource, WebSearchSource

BRIGHTPATH = "https://careers.brightpath.example/jobs/123?utm_source=google"


def sources():
    return [WebSearchSource(), RemotiveSource(), RemoteOKSource()]


def test_full_run_dedupes_scores_and_notifies(net):
    net.pages["https://careers.brightpath.example/jobs/123?utm_source=google"] = """<html><script type="application/ld+json">
    {"@type":"JobPosting","title":"Marketing Automation Specialist (HighLevel)","datePosted":"2026-11-20",
     "hiringOrganization":{"name":"Brightpath"},"employmentType":"FULL_TIME","jobLocationType":"TELECOMMUTE",
     "description":"Own our HighLevel CRM: workflows, pipelines, funnels, Zapier and webhooks integrations."}</script></html>"""
    with session_scope() as s:
        run = run_monitor(s, trigger="manual", sources=sources())
        assert run.status == "ok", run.error
        stats = run.stats
    assert stats["new"] == 4
    with session_scope() as s:
        jobs = list(s.scalars(select(Job)))
        titles = sorted(j.title for j in jobs)
        # course review filtered out; Remotive + Remote OK copies of the same role merged into one
        assert titles == [
            "CRM Automation Specialist", "GoHighLevel Automation Specialist - Acme Growth Agency",
            "Marketing Automation Specialist (HighLevel)", "We're hiring a GHL expert",
        ]
        northwind = next(j for j in jobs if j.company == "Northwind Health")
        assert sorted(x.platform for x in northwind.sources) == ["Job Boards", "Job Boards"]
        assert len(northwind.sources) == 2
        brightpath = next(j for j in jobs if j.company == "Brightpath")
        assert brightpath.employment_type == "Full-time" and brightpath.work_arrangement == "Remote"
        li = next(j for j in jobs if j.primary_platform == "LinkedIn")
        assert li.company == "Not Specified"  # never fabricated
        assert li.sources[0].platform_post_id == "li-job:3987654321"
        for j in jobs:
            assert j.score is not None and j.analyzed_by == "keyword heuristic"
            assert j.score < 90  # heuristic never claims an excellent match
            assert j.discovered_at and j.primary_url.startswith("https://")
        n_jobs = len(jobs)
    # without Claude nothing is "excellent", so instant mode sends one digest of relevant jobs
    assert len(net.emails) == 1
    assert "new GHL & automation jobs" in net.emails[0]["subject"]
    assert "remoteok.com" in net.emails[0]["html"] or "remotive.com" in net.emails[0]["html"]

    # second run: same results -> no new jobs, no new emails
    with session_scope() as s:
        run2 = run_monitor(s, trigger="manual", sources=sources())
        assert run2.stats["new"] == 0 and run2.stats["refreshed"] >= 4
    with session_scope() as s:
        assert s.scalar(select(func.count(Job.id))) == n_jobs
        assert s.scalar(select(func.count(Notification.id)).where(Notification.ok.is_(True))) == 1
    assert len(net.emails) == 1


def test_claude_analysis_path(net, monkeypatch):
    from app import config, pipeline
    from app.analysis import JobAnalysis, SubScores

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    config.reset_config()

    def fake_analyze(job, profile, prefs):
        ghl = "ghl" in job["title"].lower() or "gohighlevel" in job["title"].lower()
        return JobAnalysis(
            is_job_opportunity=True, appears_closed=False, title=job["title"], company="Not Specified",
            summary="Builds GHL automations.", required_skills=["GoHighLevel"], required_experience="Not Specified",
            salary="USD 30-40/hr", salary_min=30, salary_max=40, salary_currency="USD", salary_period="hour",
            employment_type="Contract", work_arrangement="Remote", location="Not Specified", country="Worldwide",
            application_deadline="Not Specified", application_method="DM", recruiter_info="Not Specified",
            sub_scores=SubScores(gohighlevel=95, crm_implementation=80, workflow_automation=90, marketing_automation=70,
                                 integrations=60, funnels_pipelines=80, technical_implementation=70, remote_friendly=100,
                                 compensation=50),
            relevance_score=95 if ghl else 70, matching_skills=["GHL"], missing_skills=[], red_flags=[],
            why_it_matches="GHL", recommendation="Apply now", application_strategy="Pitch fast.",
        ), 1000, 500

    monkeypatch.setattr(pipeline, "analyze_with_claude", fake_analyze)
    with session_scope() as s:
        run = run_monitor(s, trigger="manual", sources=[WebSearchSource()])
        assert run.stats["analyzed"] >= 2
    with session_scope() as s:
        excellent = list(s.scalars(select(Job).where(Job.score >= 90)))
        assert excellent and all(j.category == "Excellent Match" for j in excellent)
        assert all(j.salary_usd_annual == 72800 for j in excellent)  # 35/hr * 2080
        assert all(j.notified_instant_at is not None for j in excellent)
    subjects = [e["subject"] for e in net.emails]
    assert any("excellent" in x.lower() for x in subjects)


def test_failed_source_is_logged_not_fatal(net):
    net.serper = None  # make the handler raise
    orig = net.handler

    def broken(req):
        if req.url.host == "google.serper.dev":
            import httpx
            return httpx.Response(500, text="boom")
        return orig(req)

    from app import http
    import httpx
    http.set_client(httpx.Client(transport=httpx.MockTransport(broken)))
    with session_scope() as s:
        run = run_monitor(s, trigger="manual", sources=[WebSearchSource(), RemotiveSource()])
        assert run.status == "partial"
        assert s.scalar(select(func.count(SearchLog.id)).where(SearchLog.ok.is_(False))) > 0
        assert run.stats["new"] >= 1  # Remotive still worked


def test_disabled_platform_is_skipped(net):
    with session_scope() as s:
        p = settings_store.get(s, "platforms")
        p["Job Boards"] = False
        settings_store.put(s, "platforms", p)
        run = run_monitor(s, trigger="manual", sources=[RemotiveSource()])
        assert run.stats["skipped_sources"]["remotive"] == "platform disabled"


def test_run_once_scheduled_mode(net, monkeypatch):
    from app import run_once, worker
    from app.sources import RemotiveSource
    from app.models import MonitorRun

    monkeypatch.setattr("app.pipeline.all_sources", lambda: [RemotiveSource()])
    assert run_once.main() == 0
    with session_scope() as s:
        assert settings_store.get(s, "worker_mode") == "scheduled"
        assert s.scalar(select(func.count(MonitorRun.id))) == 1
        # next hourly run 55 minutes later still counts as due thanks to the slack
        from datetime import datetime, timedelta
        last = datetime.fromisoformat(settings_store.get(s, "last_run_started_at"))
        settings_store.put(s, "last_run_started_at", (last - timedelta(minutes=55)).isoformat())
    assert run_once.main() == 0
    with session_scope() as s:
        assert s.scalar(select(func.count(MonitorRun.id))) == 2


def test_serper_free_plan_switches_to_plain_queries(net):
    """Serper's free plan rejects site:/OR queries with a 400; the run switches to plain queries and keeps going."""
    import json as _json
    import httpx
    from app import http
    orig, sent = net.handler, []

    def free_plan(req):
        if req.url.host == "google.serper.dev":
            q = _json.loads(req.content)["q"]
            sent.append(q)
            if any(op in q for op in ("site:", " OR ", '"', " -")):
                return httpx.Response(400, json={"message": "Query pattern not allowed for free accounts.", "statusCode": 400})
        return orig(req)

    http.set_client(httpx.Client(transport=httpx.MockTransport(free_plan)))
    with session_scope() as s:
        run = run_monitor(s, trigger="manual", sources=[WebSearchSource()])
        assert settings_store.get(s, "rotation").get("serper_free_plan") == 1
        plain = [q for q in sent if "site:" not in q and " OR " not in q]
        assert 1 <= len(plain) <= 8
        assert run.stats["sources"]["web_search"] > 0
        n_failed = s.scalar(select(func.count(SearchLog.id)).where(SearchLog.ok.is_(False)))
    assert n_failed == 1  # only the first operator query was wasted
    with session_scope() as s:  # the next run goes straight to plain queries
        sent.clear()
        run_monitor(s, trigger="manual", sources=[WebSearchSource()])
        assert sent and all("site:" not in q for q in sent)
