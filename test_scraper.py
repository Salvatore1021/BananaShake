#!/usr/bin/env python
"""
APIx — Live Scraper Verification CLI
=======================================

Standalone command-line tool that proves the scraper worker actually
round-trips real data end-to-end for one (origin, destination, lead-days)
query — through the SAME compliance gate and payload parser the bulk
runner (run_daily_scrape.py) uses (no separate, untested code path) — then
prints the 5 cheapest results in a table, writes the raw parsed items to
sample_flights.json, and asserts a minimal correctness bar on the result.

    python test_scraper.py --origin DEL --destination BOM --days 7 --source akasa

--source akasa is currently the only option: Akasa Air is the one source
in compliance.SOURCE_REGISTRY that's both CLEARED (fully open robots.txt)
and confirmed working end-to-end against the real live site (not just
individually-plausible selectors) — see
apixproj/spiders/akasa_air_spider.py's module docstring for how that was
verified. Other sources (Yatra, Ixigo, Air India) were tried during this
project's development and removed once network-unreachable-from-this-
environment / UI-automation-flakiness / unconfirmed-endpoint issues made
them unreliable in practice — see git history if reviving one of them.

Also supports offline self-verification against a saved payload, so the
table/JSON/assertion logic can be proven correct without touching the
network at all:

    python test_scraper.py --replay path/to/payload.json --source akasa --origin DEL --destination BOM --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, timedelta

from app.ingestion.scheduler import SearchTask

SAMPLE_OUTPUT_PATH = "sample_flights.json"
TOP_N = 5
MIN_VALID_QUOTES = 3
REQUIRED_METADATA_FIELDS = ("airline_name", "carrier_code", "departure_time")


def build_task(origin: str, destination: str, days: int) -> SearchTask:
    departure_date = date.today() + timedelta(days=days)
    return SearchTask(
        origin=origin.upper(),
        destination=destination.upper(),
        departure_date=departure_date,
        lead_days=days,
        advance_purchase_window=f"T+{days}",
        task_id=f"cli::{origin.upper()}-{destination.upper()}::T+{days}",
    )


async def _fetch_akasa_async(task: SearchTask) -> list[dict]:
    from apixproj.spiders.akasa_air_spider import AkasaAirSpider

    spider = AkasaAirSpider(tasks=[task])
    items = [item async for item in spider.run()]
    return items


def fetch_akasa(task: SearchTask) -> list[dict]:
    """One live fetch against Akasa Air (apixproj/spiders/akasa_air_spider.py)
    — CLEARED in compliance.SOURCE_REGISTRY (fully open robots.txt), and
    confirmed working end-to-end against the real live site."""
    return asyncio.run(_fetch_akasa_async(task))


def load_replay_payload(path: str, task: SearchTask) -> list[dict]:
    """Offline path: parse a previously-saved JSON payload with the same
    parser a live run would use, so the table/JSON/assertion logic
    downstream can be proven correct without any network call."""
    from apixproj.spiders.akasa_air_spider import parse_fare_search_response

    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return parse_fare_search_response(payload, task)


def render_table(items: list[dict]) -> str:
    numeric_items = [i for i in items if isinstance(i.get("total_fare"), (int, float))]
    cheapest = sorted(numeric_items, key=lambda i: i["total_fare"])[:TOP_N]

    headers = ["Airline", "Flight #", "Base Fare", "Total Fare"]
    rows = []
    for item in cheapest:
        base_fare = item.get("base_fare")
        rows.append([
            str(item.get("airline_name") or "-"),
            str(item.get("flight_number") or "-"),
            f"{base_fare:.2f}" if isinstance(base_fare, (int, float)) else "-",
            f"{item['total_fare']:.2f}",
        ])

    if not rows:
        return "(no fare quotes with a numeric total_fare to display)"

    widths = [
        max(len(headers[c]), *(len(row[c]) for row in rows)) for c in range(len(headers))
    ]

    def format_row(cells: list[str]) -> str:
        return "| " + " | ".join(cell.ljust(widths[c]) for c, cell in enumerate(cells)) + " |"

    separator = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    lines = [separator, format_row(headers), separator]
    lines.extend(format_row(row) for row in rows)
    lines.append(separator)
    return "\n".join(lines)


def run_assertions(items: list[dict]) -> None:
    assert len(items) >= MIN_VALID_QUOTES, (
        f"expected at least {MIN_VALID_QUOTES} flight quotes, got {len(items)}"
    )

    for item in items:
        total_fare = item.get("total_fare")
        is_valid_positive_number = (
            isinstance(total_fare, (int, float)) and not isinstance(total_fare, bool) and total_fare > 0
        )
        assert is_valid_positive_number, (
            f"total_fare must be a positive number, got {total_fare!r} "
            f"for flight_number={item.get('flight_number')!r}"
        )

        for field in REQUIRED_METADATA_FIELDS:
            value = item.get(field)
            assert isinstance(value, str) and value.strip(), (
                f"{field} must be a non-empty string, got {value!r} "
                f"for flight_number={item.get('flight_number')!r}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="APIx live scraper verification CLI")
    parser.add_argument("--origin", required=True, help="origin airport code, e.g. DEL")
    parser.add_argument("--destination", required=True, help="destination airport code, e.g. BOM")
    parser.add_argument("--days", type=int, required=True, help="advance-purchase lead days (T+N)")
    parser.add_argument(
        "--source", choices=["akasa"], required=True,
        help="'akasa' = Akasa Air (the only confirmed-working source; see module docstring)",
    )
    parser.add_argument(
        "--replay", metavar="PATH",
        help="skip the live network fetch and parse a previously-saved JSON payload instead "
             "(offline self-verification of the table/JSON/assertion logic)",
    )
    args = parser.parse_args(argv)

    task = build_task(args.origin, args.destination, args.days)
    mode = f"replay of {args.replay}" if args.replay else "LIVE fetch"
    print(
        f"{mode}: {task.route_id} departing {task.departure_date.isoformat()} "
        f"({task.advance_purchase_window}) via source='{args.source}'"
    )

    items = load_replay_payload(args.replay, task) if args.replay else fetch_akasa(task)

    with open(SAMPLE_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False, default=str)
    print(f"Saved {len(items)} raw item(s) to {SAMPLE_OUTPUT_PATH}")

    print()
    print(f"Top {min(TOP_N, len(items))} cheapest:")
    print(render_table(items))
    print()

    try:
        run_assertions(items)
    except AssertionError as exc:
        print(f"VERIFICATION FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"VERIFICATION PASSED: {len(items)} quote(s), all required fields present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
