from types import SimpleNamespace

from app.board import application_message, lane_for
from app.settings_store import DEFAULTS


def test_lanes_follow_title_with_ghl_first():
    assert lane_for("GoHighLevel Automation Specialist") == "ghl"
    assert lane_for("GHL expert needed") == "ghl"
    assert lane_for("Go High Level CRM builder") == "ghl"
    assert lane_for("CRM Automation Specialist") == "automation"
    assert lane_for("Marketing automations lead") == "automation"
    assert lane_for("HubSpot CRM admin") == "automation"
    assert lane_for("Funnel builder") == "other"
    assert lane_for("Highlights editor") == "other"


def test_message_has_links_and_job_details():
    job = SimpleNamespace(title="GHL Workflow Expert", company="Acme", analysis={},
                          description="Build workflows and Zapier webhooks. Make sure it works.")
    msg = application_message(job, dict(DEFAULTS))
    assert '"GHL Workflow Expert"' in msg["text"] and "Hi Acme team" in msg["text"]
    assert "workflows, Zapier and webhooks" in msg["text"] and "Make.com" not in msg["text"]
    assert "Resume: https://drive.google.com/" in msg["text"]
    html = str(msg["html"])
    assert '<a href="https://www.loom.com/share/3699db35d27a4207b521a80b97da8e2d">Video Loom</a>' in html
    assert ">Resume</a> | <a" in html and ">Portfolio</a>" in html
    nobody = SimpleNamespace(title="x <script>", company="Not Specified", analysis={}, description="")
    m2 = application_message(nobody, dict(DEFAULTS))
    assert "Hi there" in m2["text"] and "<script>" not in str(m2["html"])
