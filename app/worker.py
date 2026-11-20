"""Background worker: runs 24/7, independent of the dashboard.

Start with:  python -m app.worker
A one-minute dispatcher reads the schedule from the database, so changes made in
Settings (frequency, digest mode, summary hour) apply without a restart."""
from __future__ import annotations

import logging
import signal
import sys
import threading
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler

from . import settings_store
from .config import get_config
from .db import init_db, session_scope
from .http import request
from .models import utcnow
from .notify import send_digest, send_system_alert
from .pipeline import maintenance, run_monitor
from .summary import daily_summary_due, email_summary, generate_daily_summary

log = logging.getLogger("worker")
_monitor_lock = threading.Lock()


def heartbeat() -> None:
    with session_scope() as s:
        settings_store.put(s, "worker_heartbeat", utcnow().isoformat())


def monitor_tick() -> None:
    """Every minute: start a run if the interval has passed or the dashboard asked for one."""
    with session_scope() as s:
        interval = int(settings_store.get(s, "monitor_interval_minutes", 60))
        last = settings_store.get(s, "last_run_started_at")
        requested = settings_store.get(s, "run_requested")
        # Scheduled (GitHub Actions) runs start a few minutes early or late, so allow 10 minutes of slack.
        slack = 10 if settings_store.get(s, "worker_mode") == "scheduled" else 0
        due = not last or utcnow() - datetime.fromisoformat(last) >= timedelta(minutes=interval - slack)
        if not (due or requested):
            return
        if not _monitor_lock.acquire(blocking=False):
            return
        settings_store.put(s, "last_run_started_at", utcnow().isoformat())
        settings_store.put(s, "run_requested", None)
    try:
        with session_scope() as s:
            run = run_monitor(s, trigger="manual" if requested else "schedule")
            log.info("run %s finished: %s %s", run.id, run.status, run.stats)
            failed_streak = _update_failure_streak(s, run.status)
        _ping_healthcheck(run.status != "failed")
        if failed_streak == 3:
            with session_scope() as s:
                send_system_alert(s, "GHL Job Hunter: 3 monitoring runs failed in a row",
                                  f"Latest error:\n{(run.error or '')[-1500:]}")
    finally:
        _monitor_lock.release()


def _update_failure_streak(s, status: str) -> int:
    streak = int(settings_store.get(s, "failed_run_streak", 0) or 0)
    streak = streak + 1 if status == "failed" else 0
    settings_store.put(s, "failed_run_streak", streak)
    return streak


def _ping_healthcheck(ok: bool) -> None:
    url = get_config().healthcheck_ping_url
    if not url:
        return
    try:
        request("GET", url if ok else url.rstrip("/") + "/fail", retries=1)
    except Exception as exc:  # pragma: no cover - network
        log.warning("healthcheck ping failed: %s", exc)


def notify_tick() -> None:
    with session_scope() as s:
        n = send_digest(s)
        if n:
            log.info("digest sent with %d jobs", n)


def summary_tick() -> None:
    with session_scope() as s:
        if not daily_summary_due(s):
            return
        summary = generate_daily_summary(s)
        if summary:
            email_summary(s, summary)
            log.info("daily summary %s generated", summary.day)


def maintenance_tick() -> None:
    with session_scope() as s:
        last = settings_store.get(s, "last_maintenance_at")
        if last and utcnow() - datetime.fromisoformat(last) < timedelta(hours=24):
            return
        settings_store.put(s, "last_maintenance_at", utcnow().isoformat())
        log.info("maintenance: %s", maintenance(s))


def _safe(fn):
    def wrapper():
        try:
            fn()
        except Exception:
            log.exception("%s failed", fn.__name__)
    wrapper.__name__ = fn.__name__
    return wrapper


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    init_db()
    with session_scope() as s:
        settings_store.ensure_defaults(s)
        settings_store.put(s, "worker_mode", "always-on")
    sched = BlockingScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
    sched.add_job(_safe(heartbeat), "interval", minutes=1, id="heartbeat", next_run_time=datetime.now())
    sched.add_job(_safe(monitor_tick), "interval", minutes=1, id="monitor", next_run_time=datetime.now())
    sched.add_job(_safe(notify_tick), "interval", minutes=5, id="digest")
    sched.add_job(_safe(summary_tick), "interval", minutes=5, id="summary")
    sched.add_job(_safe(maintenance_tick), "interval", minutes=30, id="maintenance")
    signal.signal(signal.SIGTERM, lambda *_: sched.shutdown(wait=False))
    log.info("worker started (model=%s, claude=%s, email=%s)", get_config().claude_model,
             get_config().claude_enabled, get_config().email_enabled)
    sched.start()


if __name__ == "__main__":
    main()
