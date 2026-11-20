from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

NOT_SPECIFIED = "Not Specified"

JOB_STATUSES = ["New", "Active", "Expired", "Closed"]
TRACKER_STATUSES = [
    "New", "Shortlisted", "Applied", "Interview Scheduled", "Rejected", "Offer Received", "Not Interested",
]
PLATFORMS = ["Google", "LinkedIn", "X", "Facebook", "Instagram", "Job Boards"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(500))
    company: Mapped[str] = mapped_column(String(300), default=NOT_SPECIFIED)
    description: Mapped[str] = mapped_column(Text, default="")
    required_skills: Mapped[list] = mapped_column(JSON, default=list)
    required_experience: Mapped[str] = mapped_column(String(500), default=NOT_SPECIFIED)
    salary_text: Mapped[str] = mapped_column(String(300), default=NOT_SPECIFIED)
    salary_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    salary_period: Mapped[str | None] = mapped_column(String(20), nullable=True)  # hour/month/year/project
    salary_usd_annual: Mapped[float | None] = mapped_column(Float, nullable=True)
    employment_type: Mapped[str] = mapped_column(String(50), default=NOT_SPECIFIED)
    work_arrangement: Mapped[str] = mapped_column(String(50), default=NOT_SPECIFIED)
    location: Mapped[str] = mapped_column(String(300), default=NOT_SPECIFIED)
    country: Mapped[str] = mapped_column(String(100), default=NOT_SPECIFIED)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    posted_at_text: Mapped[str | None] = mapped_column(String(100), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    content_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deadline: Mapped[str] = mapped_column(String(100), default=NOT_SPECIFIED)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    application_method: Mapped[str] = mapped_column(String(500), default=NOT_SPECIFIED)
    recruiter_info: Mapped[str] = mapped_column(String(500), default=NOT_SPECIFIED)
    status: Mapped[str] = mapped_column(String(20), default="New", index=True)

    primary_url: Mapped[str] = mapped_column(String(2000))
    primary_platform: Mapped[str] = mapped_column(String(30), index=True)
    norm_title: Mapped[str] = mapped_column(String(500), index=True)
    norm_company: Mapped[str] = mapped_column(String(300), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="")

    # AI analysis
    is_job_posting: Mapped[bool] = mapped_column(Boolean, default=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    analyzed_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    analysis_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Tracker
    tracker_status: Mapped[str] = mapped_column(String(30), default="New", index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    tracker_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Notifications
    notified_instant_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified_digest_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sources: Mapped[list["JobSource"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobSource.first_seen_at"
    )

    @property
    def platforms(self) -> list[str]:
        seen: list[str] = []
        for s in self.sources:
            if s.platform not in seen:
                seen.append(s.platform)
        return seen or [self.primary_platform]


class JobSource(Base):
    """One place a job was seen. A consolidated job keeps every source URL."""

    __tablename__ = "job_sources"
    __table_args__ = (UniqueConstraint("canonical_url", name="uq_source_canonical_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(30))
    provider: Mapped[str] = mapped_column(String(40))  # serper, serpapi_google_jobs, remotive, ...
    url: Mapped[str] = mapped_column(String(2000))
    canonical_url: Mapped[str] = mapped_column(String(2000))
    platform_post_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    query: Mapped[str | None] = mapped_column(String(500), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    job: Mapped[Job] = relationship(back_populates="sources")


class Keyword(Base):
    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String(200), unique=True)
    group: Mapped[str] = mapped_column(String(30), default="ghl")  # ghl | automation
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MonitorRun(Base):
    __tablename__ = "monitor_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running/ok/partial/failed
    trigger: Mapped[str] = mapped_column(String(20), default="schedule")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SearchLog(Base):
    __tablename__ = "search_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(40))
    platform: Mapped[str] = mapped_column(String(30))
    query: Mapped[str] = mapped_column(String(1000), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    results: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApiUsage(Base):
    __tablename__ = "api_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="request")
    units: Mapped[int] = mapped_column(Integer, default=1)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(30))  # instant/digest/daily_summary/alert
    subject: Mapped[str] = mapped_column(String(500))
    job_ids: Mapped[list] = mapped_column(JSON, default=list)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class DailySummary(Base):
    __tablename__ = "daily_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[str] = mapped_column(String(10), unique=True)  # YYYY-MM-DD in the app timezone
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    narrative: Mapped[str] = mapped_column(Text, default="")
    generated_by: Mapped[str] = mapped_column(String(60), default="")
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
