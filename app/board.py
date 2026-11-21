"""Dashboard board: sorts jobs into lanes by their title and drafts a ready-to-send application message."""
from __future__ import annotations

import re

from markupsafe import Markup, escape

from .models import NOT_SPECIFIED

# Lanes, checked in order: a title that mentions GHL wins even if it also says Automation or CRM.
GHL_RE = re.compile(r"\b(ghl|go\s*high\s*level|highlevel)\b", re.I)
AUTOMATION_RE = re.compile(r"(automat|\bcrm\b)", re.I)

LANES = [
    ("ghl", "GoHighLevel", "Job titles that mention GHL or GoHighLevel"),
    ("automation", "Automation & CRM", "Job titles that mention Automation or CRM"),
    ("other", "Other matches", "Everything else the scan found"),
]


def lane_for(title: str | None) -> str:
    title = title or ""
    if GHL_RE.search(title):
        return "ghl"
    if AUTOMATION_RE.search(title):
        return "automation"
    return "other"


DEFAULT_LINKS = {
    "resume_url": "https://drive.google.com/file/d/1C4PHkr_2S3g5gBn7ftW0YHZ2KX6Ktwc6/view?usp=sharing",
    "loom_url": "https://www.loom.com/share/3699db35d27a4207b521a80b97da8e2d",
    "portfolio_url": "https://hadiulislam.netlify.app/",
}

DEFAULT_TEMPLATE = """Hi {greeting},

I just came across your post for "{title}" and it's exactly the kind of work I do every day. I'm a GoHighLevel and automation specialist: I build sub-accounts and snapshots, pipelines, funnels and websites, workflows and triggers, calendars, forms, SMS and email campaigns, and connect GHL with Zapier, Make, n8n and webhooks.
{skills_line}
I recorded a short video walking through how I work, and you can see my resume and past projects here:
{links}

I'd be happy to jump on a quick call to talk through what you need. When would suit you?

Best regards,
{name}"""

# Skills worth naming back to the client when the posting mentions them.
SKILL_TERMS = [
    "workflows", "funnels", "websites", "snapshots", "pipelines", "calendars", "forms", "surveys",
    "SMS", "email campaigns", "reputation management", "SaaS mode", "custom values", "custom fields",
    "Zapier", "Make.com", "n8n", "Pabbly", "webhooks", "API", "Twilio", "Mailgun", "Stripe",
    "HubSpot", "Salesforce", "Pipedrive", "Keap", "ActiveCampaign", "ClickFunnels", "Kajabi",
    "lead nurturing", "appointment booking", "chatbots", "AI agents",
]
_SKILL_RES = [(t, re.compile(r"\b" + re.escape(t) + r"\b", re.I if t not in {"API", "SMS"} else 0))
              for t in SKILL_TERMS]


def matched_skills(job, limit: int = 4) -> list[str]:
    a = job.analysis or {}
    found = [s for s in (a.get("matching_skills") or []) if isinstance(s, str) and s and s != NOT_SPECIFIED]
    text = f"{job.title or ''}\n{job.description or ''}"
    for term, rx in _SKILL_RES:
        if len(found) >= limit:
            break
        if rx.search(text) and term.lower() not in {f.lower() for f in found}:
            found.append(term)
    return found[:limit]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _fill(template: str, values: dict[str, str]) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    return out


def application_message(job, prefs: dict) -> dict:
    """Returns {"text": plain text with the URLs written out, "html": rich text with hyperlinks}."""
    company = (job.company or "").strip()
    greeting = f"{company} team" if company and company != NOT_SPECIFIED else "there"
    skills = matched_skills(job)
    skills_line = f"\nI noticed you need {_join(skills)}. I've delivered that for clients many times.\n" if skills else ""
    links = [("Resume", prefs.get("resume_url") or DEFAULT_LINKS["resume_url"]),
             ("Video Loom", prefs.get("loom_url") or DEFAULT_LINKS["loom_url"]),
             ("Portfolio", prefs.get("portfolio_url") or DEFAULT_LINKS["portfolio_url"])]
    values = {"greeting": greeting, "title": job.title or "", "company": company,
              "skills_line": skills_line, "name": prefs.get("applicant_name") or ""}
    template = prefs.get("message_template") or DEFAULT_TEMPLATE
    template = re.sub(r"\n{3,}", "\n\n", template)

    text = _fill(template, {**values, "links": "\n".join(f"{n}: {u}" for n, u in links)})
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    marker = "\x00LINKS\x00"
    html_src = re.sub(r"\n{3,}", "\n\n", _fill(template, {**values, "links": marker})).strip()
    link_html = Markup(" | ").join(
        Markup('<a href="{}">{}</a>').format(u, n) for n, u in links)
    html = Markup("<br>").join(escape(line) for line in html_src.split("\n"))
    html = Markup(str(html).replace(marker, str(link_html)))
    return {"text": text, "html": html}
