"""Run the pipeline on a schedule so the database keeps filling up.

Run it:   python -m aqi_india.scheduler      (Ctrl+C to stop)

Both sources update roughly hourly, so polling every 30 minutes keeps us fresh
without hammering free APIs.
"""
from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from aqi_india.config import TIMEZONE
from aqi_india.pipeline import run_once

INTERVAL_MINUTES = 30
log = logging.getLogger(__name__)


def job() -> None:
    try:
        table = run_once()
        log.info("Run complete: %d AQI values", len(table))
    except Exception:
        log.exception("Pipeline run failed; will retry at the next interval")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    scheduler = BlockingScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        job,
        "interval",
        minutes=INTERVAL_MINUTES,
        next_run_time=datetime.now(),  # also run immediately on start
        max_instances=1,               # never overlap two runs
        coalesce=True,                 # if runs were missed (laptop asleep), do just one...
        misfire_grace_time=None,       # ...however late it is. The default (1 s) silently
                                       # DROPS late runs, so after sleep nothing ran (Q38).
    )
    # Heartbeat: a no-op every minute. The scheduler waits using a monotonic clock,
    # which on macOS does NOT advance while the computer sleeps, so after an overnight
    # sleep it would keep waiting ~30 more awake minutes before noticing the main job
    # is overdue. Waking every minute makes it re-check the real clock promptly (Q38).
    scheduler.add_job(lambda: None, "interval", minutes=1, id="heartbeat", misfire_grace_time=None, coalesce=True)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)  # don't log 1,440 heartbeats a day
    log.info("Scheduler started: every %d min. Ctrl+C to stop.", INTERVAL_MINUTES)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
