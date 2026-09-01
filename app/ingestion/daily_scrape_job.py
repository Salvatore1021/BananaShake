"""
APIx — Daily Scrape Job Scheduler
=====================================

Runs run_daily_scrape.py's full batch-scrape-and-load pipeline unattended,
once a day, exactly what the project spec calls for ("scheduled daily
extraction from airline portals") — rather than requiring someone to
remember to run `python run_daily_scrape.py` by hand every day.

Usage:

    python -m app.ingestion.daily_scrape_job

Runs in the foreground and blocks forever, firing the batch job once a day
at DAILY_SCRAPE_HOUR:DAILY_SCRAPE_MINUTE IST (defaults to 03:00 IST — a
low-traffic time for airline search endpoints, and well clear of any
midnight-boundary date-rollover edge cases in the AP-window calculation).
Leave this running — e.g. in its own terminal/tmux session, under
`pythonw`, or as a Windows Scheduled Task set to trigger "At log on" /
"At startup" so it survives reboots — and the rest is automatic.

The scheduled job calls run_daily_scrape.main() directly — the exact same
code path `python run_daily_scrape.py` uses by hand, so there is no
separately-tested "scheduled" behaviour that could drift from what running
it manually does. A crashing or failing run is caught and logged, never
allowed to kill the scheduler process itself (a bad run tomorrow shouldn't
be blocked by a bad run today).

Configuration (environment variables, both optional):
    DAILY_SCRAPE_HOUR    -- 0-23, IST. Default 3.
    DAILY_SCRAPE_MINUTE  -- 0-59, IST. Default 0.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import time
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.ingestion.scheduler import IST

logger = logging.getLogger("apix.daily_scrape_job")

JOB_ID = "daily_scrape"

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_FILE = LOG_DIR / "daily_scrape_job.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
LOG_BACKUP_COUNT = 14  # roughly two weeks of daily-job chatter at this size

# A run that comes back non-zero or raises gets this many extra attempts,
# spaced RETRY_DELAY_S apart, before the job gives up until tomorrow's
# scheduled firing. Distinguishes "Akasa's API hiccuped for ten minutes"
# (worth a same-day retry) from "something is actually broken" (retrying
# in a tight loop wouldn't help, and misfire_grace_time + coalesce on the
# cron trigger already protects against a genuinely stuck process).
MAX_RETRIES = 2
RETRY_DELAY_S = 20 * 60


def _configure_logging() -> None:
    """Console + rotating file, both at INFO. File logging is what makes
    multi-day unattended reliability actually verifiable after the fact —
    without it, a run that failed three nights ago while nobody was
    watching the console leaves no trace at all."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)


def _schedule_hour_minute() -> tuple[int, int]:
    return (
        int(os.environ.get("DAILY_SCRAPE_HOUR", "3")),
        int(os.environ.get("DAILY_SCRAPE_MINUTE", "0")),
    )


def _run_once() -> int | None:
    """One attempt at run_daily_scrape.main(). Imported lazily (not at
    module import time) so importing this module never pulls in
    Playwright/Scrapy just to build the scheduler. Returns the exit code,
    or None if main() raised (logged here, not propagated — a crash on
    attempt N must not prevent attempt N+1)."""
    import run_daily_scrape

    try:
        return run_daily_scrape.main()
    except Exception:
        logger.exception("Daily scrape attempt crashed with an unhandled exception")
        return None


def run_scrape_job() -> None:
    """One firing of the daily job, with up to MAX_RETRIES same-day
    retries (RETRY_DELAY_S apart) if the run fails outright — see those
    constants' docstring for why a retry is worth it here specifically."""
    logger.info("Daily scrape job starting")

    for attempt in range(1, MAX_RETRIES + 2):  # first attempt + MAX_RETRIES retries
        exit_code = _run_once()
        if exit_code == 0:
            logger.info("Daily scrape job finished successfully (attempt %d)", attempt)
            return

        logger.error(
            "Daily scrape attempt %d finished with exit code %s", attempt, exit_code,
        )
        if attempt <= MAX_RETRIES:
            logger.info("Retrying in %d minute(s)...", RETRY_DELAY_S // 60)
            time.sleep(RETRY_DELAY_S)

    logger.error(
        "Daily scrape job gave up after %d attempt(s) — will try again at the next scheduled run",
        MAX_RETRIES + 1,
    )


def build_scheduler() -> BlockingScheduler:
    hour, minute = _schedule_hour_minute()
    scheduler = BlockingScheduler(timezone=IST)
    scheduler.add_job(
        run_scrape_job,
        trigger=CronTrigger(hour=hour, minute=minute),
        id=JOB_ID,
        name="Daily route x AP-window fare scrape",
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    return scheduler


def main() -> None:
    _configure_logging()
    hour, minute = _schedule_hour_minute()
    scheduler = build_scheduler()
    logger.info("Daily scrape scheduler started — next run scheduled for %02d:%02d IST daily", hour, minute)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Daily scrape scheduler stopped")


if __name__ == "__main__":
    main()
