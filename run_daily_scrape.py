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

import asyncio
import json
import sys
from collections import defaultdict

from app.ingestion.scheduler import AP_WINDOWS_DAYS, ROUTE_PAIRS, RouteDateMatrixScheduler
from apixproj.raw_fare_item import write_raw_fare_items_csv

OUTPUT_JSON_PATH = "apix_daily_scrape.json"
OUTPUT_CSV_PATH = "apix_daily_scrape.csv"


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


def main() -> int:
    items, missing_summary = asyncio.run(run_batch())

    with open(OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False, default=str)
    write_raw_fare_items_csv(items, OUTPUT_CSV_PATH)

    print()
    print(f"Saved {len(items)} raw fare quote(s) to {OUTPUT_JSON_PATH} and {OUTPUT_CSV_PATH}")
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

    if not items:
        print("\nNo real data was collected across the entire basket.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
