#!/usr/bin/env python
"""
APIx — Live Scraper Verification CLI
=======================================

Standalone command-line tool that proves a single scraper worker actually
round-trips real data end-to-end for one (origin, destination, lead-days)
query — through the SAME compliance gate, header/session logic, and payload
parsers the Scrapy spiders use (no separate, untested code path) — then
prints the 5 cheapest results in a table, writes the raw parsed items to
sample_flights.json, and asserts a minimal correctness bar on the result.

    python test_scraper.py --origin DEL --destination BOM --days 7 --source ota
    python test_scraper.py --origin DEL --destination BOM --days 7 --source airline

--source ota      -> apixproj/spiders/ota_yatra_spider.py (Yatra). Yatra is
                      PENDING_LEGAL_REVIEW in compliance.SOURCE_REGISTRY, so
                      this WILL be blocked by the Compliance Gateway unless
                      APIX_ALLOW_OTA_SCRAPING=1 is set in the environment —
                      the same explicit, local-research-only opt-in the
                      scheduler and OTA spider already require (see
                      compliance.py's SOURCE_REGISTRY entry for "yatra").
                      This script does NOT set that variable itself; it must
                      be an intentional choice made outside this tool.
--source airline   -> apixproj/spiders/air_india_stealth_spider.py (Air
                      India, the one CLEARED source — no override needed).

Also supports offline self-verification against a saved payload, so the
table/JSON/assertion logic can be proven correct without touching the
network at all:

    python test_scraper.py --replay path/to/payload.json --source ota --origin DEL --destination BOM --days 7

IMPORTANT: both spiders' exact live endpoint details (Yatra's searchId
minting, Air India's fare-search API path) are marked
TODO(confirm-before-enabling) in their own modules — they have NOT been
confirmed against a live captured browser session. A live run through this
tool can therefore legitimately fail even when compliance allows it and the
network is fine, simply because the endpoint assumptions don't yet match
the real site. That is the correct, honest failure mode; this tool never
falls back to fabricated data to make a run "pass."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, timedelta

import requests

from app.ingestion.scheduler import SearchTask
from compliance import RobotsComplianceGateway

SAMPLE_OUTPUT_PATH = "sample_flights.json"
TOP_N = 5
MIN_VALID_QUOTES = 3
REQUIRED_METADATA_FIELDS = ("airline_name", "carrier_code", "departure_time")
HTTP_TIMEOUT_S = 20


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


def fetch_ota(task: SearchTask) -> list[dict]:
    """One live fetch against Yatra: search-results page -> extract
    searchId -> price endpoint -> parse JSON payload. Identical logic (same
    imported functions, not a reimplementation) to
    apixproj/spiders/ota_yatra_spider.py's YatraFareSpider, just driven
    synchronously with `requests` instead of through Scrapy's engine, since
    a one-shot CLI check doesn't need Scrapy's crawl machinery."""
    from apixproj.spiders.ota_yatra_spider import (
        PRICE_ENDPOINT_TEMPLATE,
        SEARCH_RESULTS_URL_TEMPLATE,
        SOURCE_NAME,
        _extract_search_id,
        build_realistic_headers,
        extract_yatra_fare_items,
    )

    gateway = RobotsComplianceGateway()
    search_url = SEARCH_RESULTS_URL_TEMPLATE.format(
        origin=task.origin, destination=task.destination, departure_date=task.departure_date.isoformat(),
    )
    decision = gateway.check(SOURCE_NAME, search_url)
    if not decision.allowed:
        raise SystemExit(
            f"BLOCKED by Compliance Gateway before any request was sent: {decision.reason}\n"
            "Yatra is PENDING_LEGAL_REVIEW in compliance.SOURCE_REGISTRY. To run a local "
            "verification anyway, set APIX_ALLOW_OTA_SCRAPING=1 in the environment first — "
            "see compliance.py for what that opt-in means and why it's off by default."
        )

    session = requests.Session()
    print(f"GET {search_url}")
    resp = session.get(search_url, headers=build_realistic_headers(referer="https://www.yatra.com/"), timeout=HTTP_TIMEOUT_S)
    resp.raise_for_status()

    search_id = _extract_search_id(resp.text)
    if not search_id:
        raise SystemExit(
            "Could not extract a searchId from the live search-results page. "
            "SEARCH_ID_PATTERN in ota_yatra_spider.py needs updating against this real "
            "response — this was always marked unconfirmed in that module's docstring."
        )

    price_url = PRICE_ENDPOINT_TEMPLATE.format(search_id=search_id)
    decision = gateway.check(SOURCE_NAME, price_url)
    if not decision.allowed:
        raise SystemExit(f"BLOCKED by Compliance Gateway for the price endpoint: {decision.reason}")

    print(f"GET {price_url}")
    price_resp = session.get(
        price_url,
        headers=build_realistic_headers(referer=search_url, origin_header="https://flight.yatra.com"),
        timeout=HTTP_TIMEOUT_S,
    )
    price_resp.raise_for_status()
    payload = price_resp.json()

    return extract_yatra_fare_items(payload, task)


async def _fetch_airline_async(task: SearchTask) -> list[dict]:
    from apixproj.spiders.air_india_stealth_spider import AirIndiaStealthSpider

    spider = AirIndiaStealthSpider(tasks=[task])
    items = [item async for item in spider.run()]
    return items


def fetch_airline(task: SearchTask) -> list[dict]:
    """One live fetch against Air India via the Playwright + stealth
    fallback (apixproj/spiders/air_india_stealth_spider.py) — same code
    path the standalone spider driver uses, run for exactly one task."""
    return asyncio.run(_fetch_airline_async(task))


async def _fetch_ixigo_async(task: SearchTask, debug_dir: str | None) -> list[dict]:
    from apixproj.spiders.ota_ixigo_stealth_spider import IxigoStealthSpider

    spider = IxigoStealthSpider(tasks=[task], debug_dir=debug_dir)
    items = [item async for item in spider.run()]
    return items


def fetch_ixigo(task: SearchTask, debug_dir: str | None = None) -> list[dict]:
    """One live fetch against Ixigo via the Playwright + stealth fallback
    (apixproj/spiders/ota_ixigo_stealth_spider.py) — the OTA target used
    when Yatra can't be reached at all (see that module's docstring for
    why a browser-driven approach is required here, and what's still
    unconfirmed about the exact fare-search endpoint). Pass `debug_dir` to
    save a screenshot + HTML dump on failure."""
    return asyncio.run(_fetch_ixigo_async(task, debug_dir))


async def _fetch_akasa_async(task: SearchTask) -> list[dict]:
    from apixproj.spiders.akasa_air_spider import AkasaAirSpider

    spider = AkasaAirSpider(tasks=[task])
    items = [item async for item in spider.run()]
    return items


def fetch_akasa(task: SearchTask) -> list[dict]:
    """One live fetch against Akasa Air (apixproj/spiders/akasa_air_spider.py)
    — CLEARED in compliance.SOURCE_REGISTRY (fully open robots.txt), and
    the only source in this project whose selectors and fare-search API
    are confirmed against a real captured session end-to-end rather than
    an educated guess."""
    return asyncio.run(_fetch_akasa_async(task))


def load_replay_payload(path: str, task: SearchTask, source: str) -> list[dict]:
    """Offline path: parse a previously-saved JSON payload with the same
    source-specific parser a live run would use, so the table/JSON/assertion
    logic downstream can be proven correct without any network call."""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)

    if source == "ota":
        from apixproj.spiders.ota_yatra_spider import extract_yatra_fare_items

        return extract_yatra_fare_items(payload, task)

    if source == "ixigo":
        from apixproj.spiders.ota_ixigo_stealth_spider import parse_fare_json

        return parse_fare_json(payload, task)

    if source == "akasa":
        from apixproj.spiders.akasa_air_spider import parse_fare_search_response

        return parse_fare_search_response(payload, task)

    from apixproj.spiders.air_india_stealth_spider import parse_fare_json

    return parse_fare_json(payload, task)


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
        "--source", choices=["ota", "ixigo", "akasa", "airline"], required=True,
        help="'ota' = Yatra, 'ixigo' = Ixigo (fallback OTA when Yatra is unreachable), "
             "'akasa' = Akasa Air (confirmed working end-to-end), 'airline' = Air India",
    )
    parser.add_argument(
        "--replay", metavar="PATH",
        help="skip the live network fetch and parse a previously-saved JSON payload instead "
             "(offline self-verification of the table/JSON/assertion logic)",
    )
    parser.add_argument(
        "--debug-dir", metavar="DIR",
        help="(ixigo/airline only) on a navigation/search failure, save a screenshot + full "
             "HTML dump of the page into this directory",
    )
    args = parser.parse_args(argv)

    task = build_task(args.origin, args.destination, args.days)
    mode = f"replay of {args.replay}" if args.replay else "LIVE fetch"
    print(
        f"{mode}: {task.route_id} departing {task.departure_date.isoformat()} "
        f"({task.advance_purchase_window}) via source='{args.source}'"
    )

    if args.replay:
        items = load_replay_payload(args.replay, task, args.source)
    elif args.source == "ota":
        items = fetch_ota(task)
    elif args.source == "ixigo":
        items = fetch_ixigo(task, debug_dir=args.debug_dir)
    elif args.source == "akasa":
        items = fetch_akasa(task)
    else:
        items = fetch_airline(task)

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
