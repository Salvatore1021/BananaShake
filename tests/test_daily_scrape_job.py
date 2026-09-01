"""
Unit tests for app/ingestion/daily_scrape_job.py. No real scheduler ever
runs (BlockingScheduler.start() is never called), no live network/browser
calls are made (run_daily_scrape.main is patched out), and no real delay
ever elapses (time.sleep is patched out) even though run_scrape_job()
retries on failure.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from apscheduler.triggers.cron import CronTrigger

from app.ingestion.daily_scrape_job import (
    JOB_ID,
    MAX_RETRIES,
    build_scheduler,
    run_scrape_job,
)


class BuildSchedulerTests(unittest.TestCase):
    def test_defaults_to_3am_ist_when_no_env_vars_set(self):
        env = dict(os.environ)
        env.pop("DAILY_SCRAPE_HOUR", None)
        env.pop("DAILY_SCRAPE_MINUTE", None)
        with patch.dict("os.environ", env, clear=True):
            scheduler = build_scheduler()
        job = scheduler.get_job(JOB_ID)
        self.assertIsNotNone(job)
        self.assertIsInstance(job.trigger, CronTrigger)
        fields = {f.name: str(f) for f in job.trigger.fields}
        self.assertEqual(fields["hour"], "3")
        self.assertEqual(fields["minute"], "0")

    def test_honors_env_var_overrides(self):
        with patch.dict("os.environ", {"DAILY_SCRAPE_HOUR": "6", "DAILY_SCRAPE_MINUTE": "30"}):
            scheduler = build_scheduler()
        job = scheduler.get_job(JOB_ID)
        fields = {f.name: str(f) for f in job.trigger.fields}
        self.assertEqual(fields["hour"], "6")
        self.assertEqual(fields["minute"], "30")

    def test_job_is_registered_exactly_once(self):
        scheduler = build_scheduler()
        self.assertEqual(len(scheduler.get_jobs()), 1)


class RunScrapeJobTests(unittest.TestCase):
    def _run_with_fake_main(self, main_effect):
        fake_module = MagicMock()
        if callable(main_effect) and not isinstance(main_effect, (int, MagicMock)):
            fake_module.main.side_effect = main_effect
        else:
            fake_module.main.return_value = main_effect
        with patch.dict("sys.modules", {"run_daily_scrape": fake_module}), \
             patch("app.ingestion.daily_scrape_job.time.sleep") as fake_sleep:
            run_scrape_job()
        return fake_module, fake_sleep

    def test_success_on_first_attempt_calls_main_exactly_once(self):
        fake_module, fake_sleep = self._run_with_fake_main(0)
        self.assertEqual(fake_module.main.call_count, 1)
        fake_sleep.assert_not_called()

    def test_persistent_failure_retries_up_to_max_retries_then_gives_up(self):
        fake_module, fake_sleep = self._run_with_fake_main(1)
        # first attempt + MAX_RETRIES retries
        self.assertEqual(fake_module.main.call_count, MAX_RETRIES + 1)
        # a sleep happens between attempts, not after the last one
        self.assertEqual(fake_sleep.call_count, MAX_RETRIES)

    def test_succeeds_on_a_retry_after_an_initial_failure(self):
        fake_module = MagicMock()
        fake_module.main.side_effect = [1, 0]
        with patch.dict("sys.modules", {"run_daily_scrape": fake_module}), \
             patch("app.ingestion.daily_scrape_job.time.sleep") as fake_sleep:
            run_scrape_job()  # should not raise
        self.assertEqual(fake_module.main.call_count, 2)
        self.assertEqual(fake_sleep.call_count, 1)

    def test_exception_from_main_is_caught_and_counts_as_a_failed_attempt(self):
        fake_module = MagicMock()
        fake_module.main.side_effect = RuntimeError("boom")
        with patch.dict("sys.modules", {"run_daily_scrape": fake_module}), \
             patch("app.ingestion.daily_scrape_job.time.sleep"):
            run_scrape_job()  # should not raise
        self.assertEqual(fake_module.main.call_count, MAX_RETRIES + 1)

    def test_exception_then_success_still_recovers(self):
        fake_module = MagicMock()
        fake_module.main.side_effect = [RuntimeError("boom"), 0]
        with patch.dict("sys.modules", {"run_daily_scrape": fake_module}), \
             patch("app.ingestion.daily_scrape_job.time.sleep"):
            run_scrape_job()  # should not raise
        self.assertEqual(fake_module.main.call_count, 2)


if __name__ == "__main__":
    unittest.main()
