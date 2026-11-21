"""Run every scheduled task once and exit. Used by the GitHub Actions hourly schedule
(no always-on server needed):  python -m app.run_once

Order: monitoring scan (if due) -> digest (if due) -> daily summary (if due) -> maintenance (if due)."""
from __future__ import annotations

import logging
import os
import sys

from . import settings_store
from .config import get_config, worker_config_snapshot
from .db import init_db, session_scope
from .models import utcnow
from .worker import _safe, maintenance_tick, monitor_tick, notify_tick, summary_tick

log = logging.getLogger("run_once")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    if os.environ.get("GITHUB_ACTIONS") and get_config().database_url.startswith("sqlite"):
        log.error("DATABASE_URL secret is missing: a hosted Postgres database (e.g. Neon) is required on GitHub Actions")
        return 1
    init_db()
    with session_scope() as s:
        settings_store.ensure_defaults(s)
        settings_store.put(s, "worker_mode", "scheduled")
        settings_store.put(s, "worker_heartbeat", utcnow().isoformat())
        settings_store.put(s, "worker_config", worker_config_snapshot())
    # The workflow fires hourly; monitor_tick itself skips when the Settings interval isn't up yet.
    # A "Run a scan now" request from the dashboard is picked up on the next hourly run.
    for task in (monitor_tick, notify_tick, summary_tick, maintenance_tick):
        _safe(task)()
    with session_scope() as s:
        last = settings_store.get(s, "failed_run_streak", 0) or 0
    return 1 if last else 0


if __name__ == "__main__":
    raise SystemExit(main())
