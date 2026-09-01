#!/usr/bin/env python
"""
APIx — Full Route x Date-Matrix Scrape Runner
=================================================

Runs the complete route x advance-purchase-window basket
(app/ingestion/scheduler.py: 6 city pairs x T+1/T+7/T+15/T+30/T+45 = 30
tasks) through AkasaAirSpider, collecting every real fare quote it can get
and writing them to a JSON file AND a CSV file (same data, same canonical
column order — see apixproj/raw_fare_item.py's RAW_FARE_ITEM_FIELDS) — the
"give me real data for the whole basket" counterpart to test_scraper.py's
single-query verification tool.

    python run_daily_scrape.py

Never crashes on a single task's failure. Each task is independently
gated by the Compliance Gateway and wrapped in its own try/except at every
stage inside AkasaAirSpider._run_task (request failure, non-200 response,
and payload-parsing failure are all caught there and recorded as a
missing-data flag rather than propagated) — this script's own job is just
to run the batch, print a route x window coverage summary, and save the
results. A route Akasa doesn't actually fly, or a date with no available
seats, correctly shows up as 0 quotes for that cell, not a crash — that's
real signal (missing coverage), not a bug to paper over.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
from collections import defaultdict

from app.ingestion.scheduler import AP_WINDOWS_DAYS, ROUTE_PAIRS, RouteDateMatrixScheduler
from apixproj.raw_fare_item import write_raw_fare_items_csv

OUTPUT_JSON_PATH = "apix_daily_scrape.json"
OUTPUT_CSV_PATH = "apix_daily_scrape.csv"

# A run counts as "partial" rather than "success" once missing-data flags
# (compliance blocks, request failures, empty responses, ...) touch more
# than this share of the day's task basket -- some 0-result cells are
# expected (a route genuinely has no seats that day), but a run degraded
# past this point signals something worth a human's attention rather than
# quietly rolling in as a clean success.
PARTIAL_RUN_MISSING_FRACTION = 0.2


async def run_batch() -> tuple[list[dict], dict]:
    from apixproj.spiders.akasa_air_spider import AkasaAirSpider

    scheduler = RouteDateMatrixScheduler()
    tasks = scheduler.build()
    print(f"Built {len(tasks)} tasks: {len(ROUTE_PAIRS)} routes x {len(AP_WINDOWS_DAYS)} AP windows")

    spider = AkasaAirSpider(tasks=tasks)
    items: list[dict] = []
    async for item in spider.run():
        items.append(item)
        print(
            f"  + {item['route_id']} {item['lead_window']}: "
            f"{item['flight_number']} -> Rs.{item['total_fare']:.0f}"
        )

    return items, spider.missing_data.summary()


def render_coverage_matrix(items: list[dict]) -> str:
    """Route x AP-window grid showing how many quotes were collected per
    cell. 0 means that combination returned no flights (route not served
    by this carrier, or no availability that day) — not necessarily an
    error; see module docstring."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for item in items:
        counts[(item["route_id"], item["lead_window"])] += 1

    route_ids = [route.route_id for route in ROUTE_PAIRS]
    windows = list(AP_WINDOWS_DAYS.keys())

    header = "Route".ljust(10) + "".join(w.rjust(6) for w in windows)
    lines = [header, "-" * len(header)]
    for route_id in route_ids:
        row = route_id.ljust(10)
        for window in windows:
            row += str(counts.get((route_id, window), 0)).rjust(6)
        lines.append(row)
    return "\n".join(lines)


def _run_status(items_collected: int, tasks_total: int, missing_summary: dict, db_error: str | None) -> str:
    """success/partial/failed for one ScrapeRun row -- see
    PARTIAL_RUN_MISSING_FRACTION's docstring for the partial threshold."""
    if items_collected == 0:
        return "failed"
    if db_error is not None:
        return "partial"
    missing_total = sum(missing_summary.values())
    if tasks_total > 0 and missing_total > PARTIAL_RUN_MISSING_FRACTION * tasks_total:
        return "partial"
    return "success"


def _start_scrape_run(tasks_total: int):
    """Best-effort audit row insert -- a database that's unreachable at
    the very start of a run must never block the scrape itself, so any
    failure here just means the run proceeds without an audit trail
    rather than not proceeding at all. Returns (session, run) or
    (None, None)."""
    try:
        from app.db.models import ScrapeRun
        from app.db.session import SessionLocal

        session = SessionLocal()
        run = ScrapeRun(
            started_at=datetime.datetime.now(datetime.timezone.utc),
            status="running",
            tasks_total=tasks_total,
        )
        session.add(run)
        session.commit()
        return session, run
    except Exception as exc:  # noqa: BLE001 — audit trail is best-effort, never fatal
        print(f"WARNING: could not open a ScrapeRun audit row ({exc}); proceeding without one.", file=sys.stderr)
        return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description="APIx full route x AP-window scrape runner")
    parser.add_argument(
        "--skip-db", action="store_true",
        help="write JSON/CSV only; skip loading the batch into Postgres and skip the audit trail",
    )
    args = parser.parse_args()

    tasks_total = len(ROUTE_PAIRS) * len(AP_WINDOWS_DAYS)
    run_session, scrape_run = (None, None) if args.skip_db else _start_scrape_run(tasks_total)

    items, missing_summary = asyncio.run(run_batch())

    with open(OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False, default=str)
    write_raw_fare_items_csv(items, OUTPUT_CSV_PATH)

    print()
    print(f"Saved {len(items)} raw fare quote(s) to {OUTPUT_JSON_PATH} and {OUTPUT_CSV_PATH}")

    items_loaded = 0
    db_error: str | None = None
    if not args.skip_db and items:
        from app.db.loader import load_raw_fare_items

        try:
            items_loaded = load_raw_fare_items(items)
            skipped = len(items) - items_loaded
            print(
                f"Loaded {items_loaded} new fare observation(s) into Postgres"
                + (f" ({skipped} already present, skipped)." if skipped else ".")
            )
        except Exception as exc:
            db_error = str(exc)[:2000]
            print(
                f"WARNING: Postgres load failed ({exc}); the JSON/CSV output above is still valid.",
                file=sys.stderr,
            )

        if db_error is None:
            try:
                from app.db.session import SessionLocal
                from app.index.psd_index import recompute_all

                with SessionLocal() as index_session:
                    index_rows = recompute_all(index_session)
                print(f"Recomputed {index_rows} PSD fare-index row(s).")
            except Exception as exc:
                print(f"WARNING: PSD index recompute failed ({exc}); loaded fare data is still valid.", file=sys.stderr)

    print()
    print("Coverage (quotes collected per route x AP-window):")
    print(render_coverage_matrix(items))
    print()
    if missing_summary:
        print("Missing/skipped cells (reason: count):")
        for reason, count in sorted(missing_summary.items(), key=lambda kv: -kv[1]):
            print(f"  {reason}: {count}")
    else:
        print("No missing-data flags — every task returned at least one quote.")

    if scrape_run is not None:
        try:
            scrape_run.finished_at = datetime.datetime.now(datetime.timezone.utc)
            scrape_run.status = _run_status(len(items), tasks_total, missing_summary, db_error)
            scrape_run.items_collected = len(items)
            scrape_run.items_loaded = items_loaded
            scrape_run.missing_data_summary = json.dumps(missing_summary) if missing_summary else None
            scrape_run.error_message = db_error
            run_session.add(scrape_run)
            run_session.commit()
        except Exception as exc:  # noqa: BLE001 — closing out the audit row is also best-effort
            print(f"WARNING: could not finalize the ScrapeRun audit row ({exc}).", file=sys.stderr)
        finally:
            run_session.close()

    if not items:
        print("\nNo real data was collected across the entire basket.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
