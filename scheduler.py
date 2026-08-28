"""
APIx — Ingestion Job Scheduler
================================

Turns (DGCA route basket) x (advance-purchase windows) x (compliance-cleared
sources) into today's concrete list of scrape jobs, each carrying a specific
`scheduled_time` — spread across the day so that no source ever receives
requests faster than its own politeness delay allows, no matter how many
routes/windows/intraday-samples we'd ideally like to collect.

This module deliberately treats "how many samples can we afford to collect
today" as a constraint-satisfaction problem, not a wish list: it computes the
actual slot budget a source's crawl-delay allows within the operational
window, and if the ideal target (routes x AP-windows x intraday samples)
doesn't fit, it throttles down proportionally to DGCA traffic weight —
important routes keep more samples, marginal ones lose theirs first — and
logs exactly what got cut. It never silently exceeds the politeness delay to
fit everything in.

Only sources that pass `compliance.cleared_sources()` are scheduled at all —
see compliance.py for why that list is short today.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.ingestion.compliance import RobotsComplianceGateway, cleared_sources

logger = logging.getLogger("apix.ingestion.scheduler")

# Advance-purchase windows required by the problem statement, expressed as
# day-deltas from the run date to the travel date being priced.
AP_WINDOWS_DAYS = {
    "T+1": 1,
    "T+7": 7,
    "T+15": 15,
    "T+30": 30,
    "T+45": 45,
}

# Operational scraping window — kept off the small hours deliberately. Airfare
# search traffic from real travellers is naturally near-zero at 3am; scraping
# only during hours a human plausibly would keeps our traffic pattern
# unremarkable rather than looking like a bot running on a clock 24/7, which
# is itself a courtesy to the source (and reduces the odds of tripping
# nighttime-anomaly bot heuristics for no operational benefit — nothing about
# an index needs 3am data specifically).
OPERATIONAL_WINDOW_START = time(6, 0)
OPERATIONAL_WINDOW_END = time(22, 0)

DEFAULT_INTRADAY_SAMPLES_PER_STRATUM = 3  # matches CleaningConfig's expectation of a
                                            # few intraday points per (route, source, AP-window)


@dataclass(frozen=True)
class RouteWeight:
    route_id: str          # e.g. "DEL-BOM"
    traffic_share_weight: float  # from DgcaRouteWeight, normalised to sum to 1 across the basket


@dataclass
class ScrapeJob:
    job_id: str
    route_id: str
    source_name: str
    advance_purchase_window: str
    advance_purchase_days: int
    travel_date: date
    scheduled_time: datetime
    priority: float  # higher = more important (derived from DGCA weight); used for worker-pool ordering


def _operational_window_seconds(run_date: date) -> tuple[datetime, float]:
    start_dt = datetime.combine(run_date, OPERATIONAL_WINDOW_START)
    end_dt = datetime.combine(run_date, OPERATIONAL_WINDOW_END)
    return start_dt, (end_dt - start_dt).total_seconds()


def _largest_remainder_allocation(weights: dict[str, float], total_slots: int) -> dict[str, int]:
    """Standard largest-remainder-method proportional allocation: distribute
    `total_slots` integer units across keys proportional to `weights`
    (need not sum to 1), rounding down first then handing out leftover slots
    to the largest fractional remainders. Used to turn a source's politeness-
    limited slot budget into a per-route sample count without ever exceeding
    the budget."""
    if total_slots <= 0 or not weights:
        return {k: 0 for k in weights}
    weight_sum = sum(weights.values())
    if weight_sum <= 0:
        return {k: 0 for k in weights}

    raw = {k: (w / weight_sum) * total_slots for k, w in weights.items()}
    floored = {k: int(math.floor(v)) for k, v in raw.items()}
    remainder_budget = total_slots - sum(floored.values())

    remainders = sorted(raw.keys(), key=lambda k: raw[k] - floored[k], reverse=True)
    for k in remainders[:remainder_budget]:
        floored[k] += 1
    return floored


def build_daily_job_matrix(
    routes: list[RouteWeight],
    run_date: date,
    gateway: RobotsComplianceGateway | None = None,
    intraday_samples_target: int = DEFAULT_INTRADAY_SAMPLES_PER_STRATUM,
    ap_windows: dict[str, int] | None = None,
    sources: list[str] | None = None,
    rng: random.Random | None = None,
) -> list[ScrapeJob]:
    """
    Build the full day's job list.

    Parameters
    ----------
    routes : the DGCA-weighted basket, weights normalised to sum to 1.
    run_date : the calendar day these jobs execute on.
    gateway : RobotsComplianceGateway (constructed fresh if omitted) — used
        only to determine each cleared source's crawl_delay for budgeting;
        the actual per-request robots.txt check happens again at dispatch
        time in the spider layer (robots.txt can change intraday, and the
        gateway's own cache TTL means a stale "clear" here is re-validated
        before any real request goes out).
    intraday_samples_target : ideal number of samples per (route, source,
        AP-window) stratum per day, BEFORE throttling to fit politeness
        constraints.
    ap_windows : override AP_WINDOWS_DAYS (mainly for tests).
    sources : override the live `cleared_sources()` list (mainly for tests —
        production callers should leave this as None so newly-cleared or
        newly-blocked sources take effect automatically).
    rng : injectable Random for deterministic tests.
    """
    gateway = gateway or RobotsComplianceGateway()
    ap_windows = ap_windows or AP_WINDOWS_DAYS
    sources = sources if sources is not None else cleared_sources()
    rng = rng or random.Random()

    if not sources:
        logger.warning(
            "build_daily_job_matrix: zero sources are currently CLEARED for scraping — "
            "see compliance.SOURCE_REGISTRY. No jobs will be generated today."
        )
        return []

    window_start, window_seconds = _operational_window_seconds(run_date)
    route_weight_map = {r.route_id: r.traffic_share_weight for r in routes}
    jobs: list[ScrapeJob] = []

    for source_name in sources:
        crawl_delay = gateway.crawl_delay(source_name)
        max_slots_today = int(window_seconds // crawl_delay)

        ideal_slots = len(routes) * len(ap_windows) * intraday_samples_target
        if ideal_slots > max_slots_today:
            logger.warning(
                "scheduler: source '%s' politeness budget (%d slots at %.1fs delay over "
                "%.0fs window) is below the ideal target (%d slots = %d routes x %d AP-windows "
                "x %d samples/day). Throttling proportionally to DGCA route weight.",
                source_name, max_slots_today, crawl_delay, window_seconds,
                ideal_slots, len(routes), len(ap_windows), intraday_samples_target,
            )

        # Stratum key = (route, ap_window); weight each stratum by its route's
        # DGCA traffic share (AP-windows within a route are treated equally —
        # we have no independent traffic signal to differentiate a T+1 buyer
        # from a T+30 buyer on the same route).
        stratum_weights = {
            f"{r.route_id}|{ap}": route_weight_map[r.route_id]
            for r in routes for ap in ap_windows
        }
        allocation = _largest_remainder_allocation(stratum_weights, min(max_slots_today, ideal_slots))

        # Lay out this source's allocated slots across the operational window,
        # spaced at >= crawl_delay with a small random jitter (so requests
        # don't look like a metronome, while never going faster than the
        # politeness floor). Slot order is shuffled across strata so a single
        # route's samples aren't all clustered at the start of the day.
        slot_specs: list[str] = []
        for stratum_key, n in allocation.items():
            slot_specs.extend([stratum_key] * n)
        rng.shuffle(slot_specs)

        cursor = window_start
        for i, stratum_key in enumerate(slot_specs):
            route_id, ap_window = stratum_key.split("|")
            ap_days = ap_windows[ap_window]
            scheduled_time = cursor
            # Jitter is ADDED ON TOP of the full crawl_delay for the *next*
            # slot's cursor advance — never subtracted from it — so however
            # jitter lands, consecutive requests to this domain are always
            # >= crawl_delay apart. Jittering a fixed cursor instead (i.e.
            # cursor + random offset while still advancing by exactly
            # crawl_delay) can let two adjacent jittered timestamps end up
            # CLOSER than crawl_delay, which would violate politeness.
            jitter = rng.uniform(0, max(crawl_delay * 0.25, 0.5))
            cursor = cursor + timedelta(seconds=crawl_delay + jitter)

            jobs.append(ScrapeJob(
                job_id=f"{run_date.isoformat()}::{source_name}::{route_id}::{ap_window}::{i}",
                route_id=route_id,
                source_name=source_name,
                advance_purchase_window=ap_window,
                advance_purchase_days=ap_days,
                travel_date=run_date + timedelta(days=ap_days),
                scheduled_time=scheduled_time,
                priority=route_weight_map[route_id],
            ))

        logger.info(
            "scheduler: source '%s' -> %d jobs scheduled today (%.1fs politeness delay, "
            "%d/%d of ideal target achieved)",
            source_name, len(slot_specs), crawl_delay, len(slot_specs), ideal_slots,
        )

    jobs.sort(key=lambda j: j.scheduled_time)
    return jobs
