"""
APIx — PSD Airfare Price Index Construction
================================================

Turns the daily per-route fare quotes in fare_observations into a single
daily index number, the way a statistical office builds a weighted
price-relative index over a fixed basket:

  1. Every quote collected for one route, on one day, in one AP window is
     first collapsed into a single ELEMENTARY fare for that
     (route, date, lead_window) cell — see ELEMENTARY METHODS below for
     how, and why that choice matters.
  2. Each route gets its own BASE DATE — the earliest date that route has
     an elementary fare for — and every later day's elementary fare on
     that route is expressed as a ratio to its own base-date fare (a
     "price relative"). Anchoring per-route rather than to one shared
     calendar date means a route added to the basket later still gets a
     valid base instead of being excluded until history catches up, and it
     means routes with very different absolute fares (a short hop vs. a
     long one) contribute comparably — the index isn't dominated by
     whichever route happens to cost the most in rupees.
  3. Each route's price relative is weighted by its configured weight (see
     app/index/weights.py), renormalized across whichever routes actually
     have data on that day — see normalized_weights()'s own docstring for
     why that matters.
  4. INDEX(date) = 100 * sum(weight_r * price_relative_r) — exactly 100 on
     a route's own base date by construction, moving proportionally to the
     weighted-average fare change since then.

Computed per (date, lead_window) plus one blended "overall" row per date
(equal-weighted across whichever AP windows have a value that date), and
persisted to fare_index_daily (app/db/models.py) rather than recomputed on
every API request. recompute_all() is idempotent — safe to call after
every daily load, and safe to re-run standalone at any time (e.g. after
editing weights.py) since every write is an upsert keyed on
(index_date, lead_window, method).

ELEMENTARY METHODS — arithmetic (Carli) vs. geometric (Jevons)
----------------------------------------------------------------
Step 1 above (several same-day quotes for one route -> one elementary
fare) is exactly the "elementary aggregate" problem every national
statistical office's price index has to solve at its lowest level, and it
has a well-documented right answer:

  * carli_arithmetic_v1 — the ORIGINAL formula this project shipped with —
    is a plain arithmetic mean of the day's quotes. This is structurally a
    "Carli index" at the elementary level. Carli/arithmetic-mean-of-
    relatives is well known in index-number theory to carry a persistent
    UPWARD bias versus a geometric-mean aggregate whenever the underlying
    quotes are dispersed (different fare classes/flight times on the same
    route quoting quite different fares, which the fare heatmap on this
    same dashboard shows is routinely the case here) — see the IMF's CPI
    Manual (ch. 5, "Elementary Indices") and Balk (2005)/ILO-endorsed
    literature: "most papers recommend the Jevons index rather than the
    Carli index... the Jevons index is clearly the index with the best
    properties" from an axiomatic standpoint.
  * jevons_geometric_v2 — the RECOMMENDED replacement — takes the
    geometric mean of the same day's quotes instead (exp(avg(ln(fare)))).
    This is not a theoretical nicety for this specific project: India's
    own Office of Economic Adviser made exactly this change to the
    Wholesale Price Index, replacing "the practice of taking arithmetic
    mean of price relatives (Carli Index)" with "the geometric mean of
    the price relatives (Jevons' Index)... for convergence of methods used
    in index compilation by the government" (WPI Manual, base 2011-12) —
    the same fix, for the same reason, on the same country's official
    price statistics this project's DGCA-oriented framing already takes
    as its reference point.

  Deliberately UNCHANGED by this: the weighted combination ACROSS routes
  (step 3-4) stays a weighted arithmetic mean of each route's price
  relative. That is not an oversight -- it mirrors the same WPI/CPI
  practice at the next stage up ("these elementary price indices are
  aggregated using weighted arithmetic mean... using Laspeyres index
  formula"), so this module's two stages match the real, published,
  two-stage structure end to end rather than applying a geometric mean
  somewhere it isn't the documented fix.

  Also deliberately REJECTED: switching each route's fixed per-route base
  date to a day-over-day CHAIN-LINKED index. Chain-linking is the right
  answer for a basket that turns over slowly (a yearly CPI re-weight), but
  the IMF's CPI Manual (ch. 6-7, "Chain Drift Problem") is explicit that
  "high-frequency chaining of weighted price indices...can lead to strong
  chain drift," and this index recomputes DAILY against fares that airline
  revenue management moves for reasons that have nothing to do with
  underlying cost (seat scarcity, day-of-week, promotions) — precisely the
  bounce-prone conditions the chain-drift warning is about. A fixed
  per-route base avoids that failure mode entirely, so it stays.

Both methods are computed and persisted side by side (see
ACTIVE_INDEX_METHOD below) rather than one replacing the other in the
database — recompute_all() writes both every time it runs, so the
non-active method's series is never stale and never lost. Switching which
one the API/dashboard serves is the one-line ACTIVE_INDEX_METHOD flip
below; no migration, backfill, or data loss either way.

MULTI-ROUTE COMBINATION METHODS — bilateral (Törnqvist) vs. multilateral (GEKS)
--------------------------------------------------------------------------------
Everything above is about STAGE 1 (collapsing one cell's several same-day
quotes into one elementary fare). Two further methods below instead change
STAGE 3 (how routes are COMBINED into one blended index) — each of them
still uses the recommended jevons_geometric_v2 elementary fare as its
input (that choice is settled by the section above), so what varies is
purely the aggregation-across-routes formula:

  * tornqvist_bilateral_v1 — a proper Törnqvist index: a weighted
    GEOMETRIC mean of every route's price relative (today's elementary
    fare / that route's own base-day elementary fare), instead of the
    weighted ARITHMETIC mean jevons_geometric_v2 uses at this stage:

        ln(INDEX/100) = sum_r( w_r * ln(fare_r(t) / fare_r(base_r)) )

    Weighted geometric averaging of price relatives is exactly the
    Törnqvist formula (Törnqvist, 1936) — one of the two "superlative"
    index formulas the IMF's CPI Manual (ch. 18) singles out as best
    approximating the true cost-of-living index to the second order, and
    the standard bilateral (exactly-two-periods-compared) alternative to
    a Laspeyres-style weighted arithmetic mean. `w_r` reuses this
    project's existing normalized route weights (see weights.py) as the
    expenditure-share proxy the formula calls for — the project has no
    real per-route passenger-volume data yet (weights.py's own docstring
    already flags this same gap for the arithmetic combination), so both
    combination formulas share that one limitation equally rather than
    Törnqvist introducing a new one.

  * geks_multilateral_v1 — a GEKS index (Gini-Éltető-Köves-Szulc): fixes
    the one real weakness a BILATERAL index (Törnqvist above, or the
    existing per-route-fixed-base design) has whenever the set of routes
    reporting a fare changes from day to day, which real scraped fare
    data does routinely (a route can simply have no quote on a given
    day). A bilateral index only ever compares two periods directly; if
    the route panel differs across periods, direct pairwise comparisons
    are not guaranteed to be MULTILATERALLY CONSISTENT (transitive) —
    comparing day A to day C directly can disagree with going A -> B -> C.
    GEKS is the standard fix used for exactly this kind of unbalanced
    panel (see IMF CPI Manual ch. 8, "Multilateral Index Number Methods",
    and the scanner-data GEKS literature, e.g. Ivancic/Diewert/Fox 2011):
    build the bilateral Törnqvist index between EVERY pair of dates
    (using only the routes both dates share a fare for), then average
    each date's whole chain of bilateral comparisons geometrically:

        ln(INDEX(t)/100) = (1/|D|) * sum_k( ln P(k,t) - ln P(k,base) )

    where D is every date with data for this AP window, P(r,s) is the
    bilateral Törnqvist index from date r to date s (see
    _bilateral_ln_relative), and `base` is the single earliest date
    across the whole series (a shared multilateral reference point,
    unlike the per-route fixed bases the other three methods use). This
    is more compute (every date is compared against every other date)
    but is exact, not an approximation, and at this project's data
    volume (low hundreds of day/window cells) trivially fast.

Every one of the four methods above is computed from the SAME underlying
fare_observations rows — there is no synthetic or estimated data anywhere
in this module. A method's full historical series is exactly as long as
the real scraped data allows, recomputed from scratch (not chained
forward from a placeholder) every time recompute_all() runs.
"""

from __future__ import annotations

import datetime
import logging
from decimal import Decimal

from sqlalchemy import Date, cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import FareIndexDaily, FareObservation
from app.index.weights import normalized_weights

logger = logging.getLogger("apix.index.psd")

BASE_INDEX_VALUE = Decimal("100")
QUANT = Decimal("0.0001")

# The four index-construction methods this module can compute (see the
# module docstring's ELEMENTARY METHODS and MULTI-ROUTE COMBINATION
# METHODS sections for the research behind each). All four are real,
# permanent method names persisted on every row (FareIndexDaily.method)
# -- never renamed or reused for a different formula later, since
# existing rows carry these strings as their meaning.
LEGACY_METHOD = "carli_arithmetic_v1"           # arithmetic mean of same-day quotes (original formula)
RECOMMENDED_METHOD = "jevons_geometric_v2"      # geometric mean of same-day quotes (recommended replacement)
TORNQVIST_METHOD = "tornqvist_bilateral_v1"     # weighted geometric mean of routes' price relatives (bilateral, per-route base)
GEKS_METHOD = "geks_multilateral_v1"            # GEKS multilateral aggregation over every pairwise date comparison
ALL_METHODS = (LEGACY_METHOD, RECOMMENDED_METHOD, TORNQVIST_METHOD, GEKS_METHOD)

# The one line that decides which method recompute_all() treats as "the"
# index (see persist behaviour below) and which one the API serves by
# default (app/api/routers/index.py's GET /index/daily with no ?method=).
# Reverting the formula change is flipping this back to LEGACY_METHOD --
# both methods' rows already exist in fare_index_daily at every moment
# (recompute_all() persists ALL_METHODS every run), so this alone is a
# complete, instant, zero-data-loss revert. Nothing else needs to change.
ACTIVE_INDEX_METHOD = RECOMMENDED_METHOD

# Sentinel lead_window value for the one blended "overall" row per date —
# a real string, deliberately not SQL NULL. See FareIndexDaily's docstring
# (app/db/models.py) for why: Postgres's ON CONFLICT never matches two
# NULLs as equal, so a nullable lead_window would make every recompute
# insert a fresh overall row instead of updating the existing one.
OVERALL_LABEL = "OVERALL"


def _elementary_agg(method: str):
    """The SQL aggregate expression that collapses one (route, date,
    lead_window) cell's several quotes into one elementary fare -- the
    module docstring's ELEMENTARY METHODS section is the reasoning for why
    there are two of these and why they aren't interchangeable cosmetics.

    jevons_geometric_v2's exp(avg(ln(x))) is the standard log-mean-exp
    identity for a geometric mean: there's no native GEOMETRIC_MEAN
    aggregate in Postgres, but avg() of the logs and exponentiating the
    result is exact (not an approximation) for positive fares, which every
    total_fare here is (enforced by the domain, not by a CHECK constraint,
    but this module already filters total_fare IS NOT NULL below and a
    fare of <= 0 would never come from a real quote).

    Every method other than the original LEGACY_METHOD uses this same
    geometric elementary fare -- tornqvist_bilateral_v1 and
    geks_multilateral_v1 (see the module docstring's MULTI-ROUTE
    COMBINATION METHODS section) change how routes are combined, not how
    one route's own same-day quotes are collapsed, so they inherit the
    already-settled, already-documented elementary fix rather than
    reopening it."""
    if method == LEGACY_METHOD:
        return func.avg(FareObservation.total_fare)
    return func.exp(func.avg(func.ln(FareObservation.total_fare)))


def _daily_avg_fares(
    session: Session, lead_window: str | None = None, method: str = ACTIVE_INDEX_METHOD,
) -> dict[tuple[datetime.date, str, str], Decimal]:
    """(date, route_id, lead_window) -> elementary fare across every loaded
    observation for that cell, aggregated per `method` (see
    _elementary_agg). NOTE: this is deliberately its own aggregation, not a
    reuse of GET /fares/daily-summary's avg_total_fare -- that endpoint is
    a plain arithmetic summary for the route table/heatmap (which want the
    literal mean fare shown to a person), while this is the index's own
    elementary aggregate and the two are allowed to diverge now that the
    index has a second, geometric formula."""
    fare_date = cast(FareObservation.scraped_at, Date).label("fare_date")
    stmt = (
        select(
            fare_date,
            FareObservation.route_id,
            FareObservation.lead_window,
            _elementary_agg(method).label("avg_total_fare"),
        )
        .where(FareObservation.total_fare.is_not(None))
        .group_by(fare_date, FareObservation.route_id, FareObservation.lead_window)
    )
    if lead_window is not None:
        stmt = stmt.where(FareObservation.lead_window == lead_window)
    return {
        (row.fare_date, row.route_id, row.lead_window): row.avg_total_fare
        for row in session.execute(stmt)
    }


def _base_fares(
    daily: dict[tuple[datetime.date, str, str], Decimal], lead_window: str,
) -> dict[str, tuple[datetime.date, Decimal]]:
    """Each route's own earliest (date, avg_fare) for one AP window — the
    anchor its price relative is computed against on every later date."""
    bases: dict[str, tuple[datetime.date, Decimal]] = {}
    for (fdate, route_id, window), avg_fare in daily.items():
        if window != lead_window:
            continue
        current = bases.get(route_id)
        if current is None or fdate < current[0]:
            bases[route_id] = (fdate, avg_fare)
    return bases


def _route_fares_by_date(
    daily: dict[tuple[datetime.date, str, str], Decimal],
    lead_window: str,
    route_filter: set[str] | None = None,
) -> dict[datetime.date, dict[str, Decimal]]:
    """Reshapes _daily_avg_fares's (date, route, window) -> fare mapping
    into date -> {route_id: fare} for one AP window — the shape both
    compute_window_index_series (route vs. its own fixed base) and
    compute_geks_window_index_series (route vs. every other date) build
    their per-day route sets from. `route_filter`, when given, drops any
    route that never established a base at all (e.g. it never has more
    than the one already-excluded null fare) rather than letting it
    silently contribute to a day's normalized weights."""
    by_date: dict[datetime.date, dict[str, Decimal]] = {}
    for (fdate, route_id, window), avg_fare in daily.items():
        if window != lead_window:
            continue
        if route_filter is not None and route_id not in route_filter:
            continue
        by_date.setdefault(fdate, {})[route_id] = avg_fare
    return by_date


def compute_window_index_series(
    session: Session,
    lead_window: str,
    weights: dict[str, float] | None = None,
    method: str = ACTIVE_INDEX_METHOD,
) -> list[dict]:
    """Daily index rows for one AP window across every date that has data,
    each date compared directly (BILATERALLY) against that route's own
    fixed base date. A pure read + compute — nothing here writes to the
    database (see persist_index_rows for that).

    `method` changes how a (route, date, window) cell's own quotes are
    collapsed into one elementary fare (_daily_avg_fares) for every
    method, and additionally changes how routes are COMBINED for
    TORNQVIST_METHOD (weighted geometric mean of price relatives, the
    Törnqvist formula) vs. every other method here (weighted arithmetic
    mean) — see the module docstring's ELEMENTARY METHODS and MULTI-ROUTE
    COMBINATION METHODS sections for why each split is deliberate.
    GEKS_METHOD is NOT handled here at all — its multilateral comparison
    needs every date at once, not one date against its own fixed base, so
    it has its own compute_geks_window_index_series below."""
    daily = _daily_avg_fares(session, lead_window, method)
    bases = _base_fares(daily, lead_window)
    if not bases:
        return []

    by_date = _route_fares_by_date(daily, lead_window, route_filter=set(bases))

    rows: list[dict] = []
    for fdate in sorted(by_date):
        day_fares = by_date[fdate]
        norm_weights = normalized_weights(list(day_fares.keys()), weights)
        if not norm_weights:
            continue
        if method == TORNQVIST_METHOD:
            # Törnqvist: ln(index/100) = sum_r( w_r * ln(price_relative_r) )
            # -- a weighted GEOMETRIC mean of the routes' price relatives
            # in place of the weighted arithmetic mean below.
            log_weighted_sum = sum(
                (Decimal(str(norm_weights[route_id])) * (day_fares[route_id] / bases[route_id][1]).ln())
                for route_id in day_fares
                if route_id in norm_weights
            )
            index_value = (log_weighted_sum.exp() * BASE_INDEX_VALUE).quantize(QUANT)
        else:
            weighted_sum = sum(
                (Decimal(str(norm_weights[route_id])) * (day_fares[route_id] / bases[route_id][1]))
                for route_id in day_fares
                if route_id in norm_weights
            )
            index_value = (weighted_sum * BASE_INDEX_VALUE).quantize(QUANT)
        rows.append(
            {
                "index_date": fdate,
                "lead_window": lead_window,
                "method": method,
                "index_value": index_value,
                "base_date": min(base_date for base_date, _ in bases.values()),
                "route_count": len(day_fares),
            }
        )
    return rows


def _bilateral_ln_relative(
    fares_by_date: dict[datetime.date, dict[str, Decimal]],
    date_r: datetime.date,
    date_s: datetime.date,
    weights: dict[str, float] | None,
) -> Decimal | None:
    """ln of the bilateral Törnqvist index comparing exactly date_r to
    date_s: a weighted geometric mean of each route's price relative
    between these two dates, using only the routes that reported a fare
    on BOTH (the matched sample) -- renormalized to just that overlap,
    the same "redistribute weight across whoever actually has data"
    principle normalized_weights() already applies within a single date.

    Returns None when the two dates share no route at all (nothing to
    compare), which the caller treats as "this date pair can't bridge the
    GEKS comparison" rather than a zero.

    Symmetric by construction: swapping date_r/date_s negates every
    ln(relative) term (ln(a/b) = -ln(b/a)) while the weights are
    unchanged (same route set, same table), so
    _bilateral_ln_relative(r, s) == -_bilateral_ln_relative(s, r) exactly
    -- the time-reversal property a bilateral index formula is expected
    to satisfy."""
    routes_r = fares_by_date.get(date_r, {})
    routes_s = fares_by_date.get(date_s, {})
    common = [rid for rid in routes_r if rid in routes_s]
    if not common:
        return None
    norm_weights = normalized_weights(common, weights)
    if not norm_weights:
        return None
    return sum(
        Decimal(str(norm_weights[rid])) * (routes_s[rid] / routes_r[rid]).ln()
        for rid in common
    )


def compute_geks_window_index_series(
    session: Session,
    lead_window: str,
    weights: dict[str, float] | None = None,
) -> list[dict]:
    """Daily GEKS index rows for one AP window — see the module
    docstring's MULTI-ROUTE COMBINATION METHODS section for the full
    formula and the unbalanced-panel problem it solves. Every date with
    data for this window is compared bilaterally (Törnqvist) against
    every other such date, and a date's final index value is the
    geometric mean of its whole chain of bilateral comparisons against
    the single earliest date -- this is what makes the series
    MULTILATERALLY consistent even on days where a different subset of
    routes happened to report, unlike the fixed-per-route-base methods
    above.

    O(dates^2) bilateral comparisons per window -- exact, not sampled or
    windowed, since this project's per-window date count (one row per
    calendar day the scraper has run) stays small enough for that to be
    trivially fast rather than a real cost."""
    daily = _daily_avg_fares(session, lead_window, RECOMMENDED_METHOD)
    fares_by_date = _route_fares_by_date(daily, lead_window)
    dates = sorted(fares_by_date)
    if not dates:
        return []

    base_date = dates[0]
    # Precomputed once per bridge date k -- reused for every target date t
    # below instead of recomputed inside the O(dates^2) loop.
    ln_k_to_base = {k: _bilateral_ln_relative(fares_by_date, k, base_date, weights) for k in dates}

    rows: list[dict] = []
    for target_date in dates:
        ln_terms = []
        for bridge_date in dates:
            ln_bridge_to_base = ln_k_to_base[bridge_date]
            if ln_bridge_to_base is None:
                continue
            ln_bridge_to_target = _bilateral_ln_relative(fares_by_date, bridge_date, target_date, weights)
            if ln_bridge_to_target is None:
                continue
            ln_terms.append(ln_bridge_to_target - ln_bridge_to_base)
        if not ln_terms:
            continue
        ln_geks = sum(ln_terms) / len(ln_terms)
        index_value = (ln_geks.exp() * BASE_INDEX_VALUE).quantize(QUANT)
        rows.append(
            {
                "index_date": target_date,
                "lead_window": lead_window,
                "method": GEKS_METHOD,
                "index_value": index_value,
                "base_date": base_date,
                "route_count": len(fares_by_date[target_date]),
            }
        )
    return rows


def compute_overall_index_series(window_series_by_window: dict[str, list[dict]], method: str = ACTIVE_INDEX_METHOD) -> list[dict]:
    """One blended row per date, equal-weighted across whichever AP
    windows have a value on that date — lead_window=OVERALL_LABEL marks it
    as the blended row rather than a per-window one (see FareIndexDaily's
    docstring for why this is a string sentinel, not NULL)."""
    by_date: dict[datetime.date, list[dict]] = {}
    for rows in window_series_by_window.values():
        for row in rows:
            by_date.setdefault(row["index_date"], []).append(row)

    overall: list[dict] = []
    for fdate in sorted(by_date):
        day_rows = by_date[fdate]
        avg_value = (sum(r["index_value"] for r in day_rows) / len(day_rows)).quantize(QUANT)
        overall.append(
            {
                "index_date": fdate,
                "lead_window": OVERALL_LABEL,
                "method": method,
                "index_value": avg_value,
                "base_date": min(r["base_date"] for r in day_rows),
                "route_count": sum(r["route_count"] for r in day_rows),
            }
        )
    return overall


def persist_index_rows(session: Session, rows: list[dict]) -> int:
    """Idempotent upsert into fare_index_daily, keyed on
    (index_date, lead_window, method) — recomputing after new data lands
    just overwrites that date+method's value rather than duplicating rows,
    while a *different* method's rows are a different key entirely and are
    never touched by this (see FareIndexDaily's docstring)."""
    if not rows:
        return 0
    stmt = pg_insert(FareIndexDaily).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_fare_index_daily_date_window_method",
        set_={
            "index_value": stmt.excluded.index_value,
            "base_date": stmt.excluded.base_date,
            "route_count": stmt.excluded.route_count,
        },
    )
    session.execute(stmt)
    return len(rows)


def recompute_all(
    session: Session,
    weights: dict[str, float] | None = None,
    ap_windows: list[str] | None = None,
    commit: bool = True,
    methods: tuple[str, ...] | None = None,
) -> int:
    """Recompute and persist the full index series: one row per
    (date, lead_window, method) for every AP window with data plus the
    blended overall row per date, for every method in `methods` (default
    ALL_METHODS — i.e. every run keeps every method's series fully up to
    date purely from the real fare_observations on hand, so
    ACTIVE_INDEX_METHOD can be flipped back at any moment, or any method
    inspected directly, without that series ever having gone stale or
    ever having been backfilled with anything other than a real
    recomputation). Called after every daily load (run_daily_scrape.py)
    and safe to re-run standalone at any time."""
    from app.ingestion.scheduler import AP_WINDOWS_DAYS

    windows = ap_windows if ap_windows is not None else list(AP_WINDOWS_DAYS.keys())
    methods_to_run = methods if methods is not None else ALL_METHODS

    total = 0
    for method in methods_to_run:
        if method == GEKS_METHOD:
            # Multilateral -- needs every date at once, not one date
            # against its own fixed base, so it has its own compute
            # function rather than compute_window_index_series's per-date
            # loop (see compute_geks_window_index_series's docstring).
            series_by_window = {w: compute_geks_window_index_series(session, w, weights) for w in windows}
        else:
            series_by_window = {w: compute_window_index_series(session, w, weights, method) for w in windows}
        overall_rows = compute_overall_index_series(series_by_window, method)

        for rows in series_by_window.values():
            total += persist_index_rows(session, rows)
        total += persist_index_rows(session, overall_rows)

    if commit:
        session.commit()
    logger.info("psd_index: recomputed and persisted %d index row(s) across %d method(s)", total, len(methods_to_run))
    return total
