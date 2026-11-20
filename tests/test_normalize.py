from datetime import datetime

from app.normalize import canonical_url, detect_platform, norm_title, parse_date, platform_post_id
from app.enrich import parse_jobposting_html


def test_canonical_url_strips_tracking_and_normalises_social():
    assert canonical_url("https://pk.linkedin.com/jobs/view/ghl-expert-at-acme-3987654321?trk=x&refId=1") == \
        "https://linkedin.com/jobs/view/3987654321"
    assert canonical_url("https://twitter.com/abc/status/123456?s=20") == "https://x.com/i/status/123456"
    assert canonical_url("https://www.Example.com/jobs/1/?utm_source=g&id=5#apply") == "https://example.com/jobs/1?id=5"


def test_platform_detection_and_ids():
    assert detect_platform("https://www.facebook.com/groups/ghl/posts/998877") == "Facebook"
    assert detect_platform("https://instagram.com/p/AbC123/") == "Instagram"
    assert detect_platform("https://careers.acme.com/1") is None
    assert platform_post_id("https://facebook.com/groups/ghl/posts/998877") == "fb-group:998877"
    assert platform_post_id("https://x.com/a/status/42") == "x:42"


def test_title_normalisation_merges_ghl_spellings():
    assert norm_title("Go High Level Expert (Remote)") == norm_title("GHL Expert") == "gohighlevel expert"


def test_parse_date_relative_iso_and_garbage():
    now = datetime(2026, 11, 20, 12, 0)
    assert parse_date("3 hours ago", now) == datetime(2026, 11, 20, 9, 0)
    assert parse_date("2026-11-19T10:00:00Z", now) == datetime(2026, 11, 19, 10, 0)
    assert parse_date("not a date", now) is None
    assert parse_date(None, now) is None


def test_jsonld_jobposting_extraction():
    html = """<html><script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting",
    "title":"GHL Developer","datePosted":"2026-11-18","validThrough":"2027-01-01T00:00",
    "hiringOrganization":{"@type":"Organization","name":"Acme"},"employmentType":"CONTRACTOR",
    "jobLocationType":"TELECOMMUTE","description":"<p>Build GoHighLevel workflows</p>",
    "baseSalary":{"@type":"MonetaryAmount","currency":"USD","value":{"@type":"QuantitativeValue","minValue":25,"maxValue":40,"unitText":"HOUR"}}}
    </script></html>"""
    info = parse_jobposting_html(html)
    assert info.title == "GHL Developer" and info.company == "Acme"
    assert info.employment_type == "Contract" and info.work_arrangement == "Remote"
    assert info.salary_text == "USD 25 - 40 per hour"
    assert info.deadline_at.year == 2027
    assert not info.closed
    assert parse_jobposting_html("<p>This job is no longer accepting applications</p>").closed
