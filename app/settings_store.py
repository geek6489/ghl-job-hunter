"""User-editable settings, stored in the database so the dashboard can change them
and the worker picks them up on its next tick."""
from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .board import DEFAULT_LINKS, DEFAULT_TEMPLATE
from .models import Keyword, Setting, utcnow

DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "ghl": [
        "GHL Expert", "GHL Specialist", "GoHighLevel Expert", "GoHighLevel Specialist",
        "GoHighLevel Automation Expert", "GoHighLevel Automation Specialist", "Go High Level Expert",
        "Go High Level Specialist", "GoHighLevel CRM Specialist", "GoHighLevel Developer",
        "GHL Automation Consultant", "GoHighLevel Workflow Expert",
    ],
    "automation": [
        "Process Automation Specialist", "Marketing Automation Specialist", "CRM Automation Specialist",
        "Workflow Automation Expert", "CRM Implementation Specialist", "Marketing Operations Specialist",
        "Automation Consultant", "Business Process Automation Expert",
    ],
}

DEFAULT_PROFILE = (
    "GoHighLevel (GHL / HighLevel / LeadConnector) specialist. Builds sub-accounts, snapshots, pipelines, "
    "funnels and websites, workflows and triggers, calendars, forms and surveys, SMS/email campaigns, "
    "custom fields and values, reputation management, and integrations with Zapier, Make, Pabbly and n8n, "
    "webhooks and APIs. CRM implementation and marketing automation for agencies and SMBs. Prefers remote work."
)

DEFAULTS: dict[str, Any] = {
    "platforms": {
        "Google": True, "LinkedIn": True, "X": True, "Facebook": True, "Instagram": True, "Job Boards": True,
    },
    "monitor_interval_minutes": 60,
    "notification_frequency": "instant",  # instant | 3h | daily
    "email_enabled": True,
    "email_to": "",  # falls back to EMAIL_TO env
    "instant_min_score": 90,
    "min_relevance_score": 60,  # minimum score that gets emailed / shown by default
    "daily_summary_hour": 8,  # local hour in APP_TIMEZONE
    "digest_hour": 9,  # for the daily digest mode
    "preferred_locations": ["Remote"],
    "remote_only": False,
    "salary_min_usd_annual": None,
    "salary_min_hourly_usd": None,
    "max_job_age_days": 30,
    "profile": DEFAULT_PROFILE,
    # Ready-to-send application message shown on every job card
    "applicant_name": "Hadi Ul Islam",
    **DEFAULT_LINKS,
    "message_template": DEFAULT_TEMPLATE,
}


def ensure_defaults(session: Session) -> None:
    existing = {s.key for s in session.scalars(select(Setting))}
    for key, value in DEFAULTS.items():
        if key not in existing:
            session.add(Setting(key=key, value=copy.deepcopy(value)))
    if session.scalar(select(Keyword.id).limit(1)) is None:
        for group, words in DEFAULT_KEYWORDS.items():
            for w in words:
                session.add(Keyword(text=w, group=group, enabled=True))
    session.flush()


def get(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(Setting, key)
    if row is None:
        if key in DEFAULTS:
            return copy.deepcopy(DEFAULTS[key])
        return default
    return row.value


def get_all(session: Session) -> dict[str, Any]:
    values = copy.deepcopy(DEFAULTS)
    for row in session.scalars(select(Setting)):
        values[row.key] = row.value
    # make sure newly added platforms appear even on old databases
    plats = dict(DEFAULTS["platforms"])
    plats.update(values.get("platforms") or {})
    values["platforms"] = plats
    return values


def put(session: Session, key: str, value: Any) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()
    session.flush()


def enabled_keywords(session: Session) -> list[Keyword]:
    return list(session.scalars(select(Keyword).where(Keyword.enabled.is_(True)).order_by(Keyword.id)))
