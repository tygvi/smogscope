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
        coalesce=True,                 # if runs were missed (laptop asleep), do just one
    )
    log.info("Scheduler started: every %d min. Ctrl+C to stop.", INTERVAL_MINUTES)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
