"""
APIx — Route Weights for the PSD Fare Index
================================================

The weight each route contributes to the blended Airfare Price Index.
Every route in app.ingestion.scheduler.ROUTE_PAIRS gets an explicit, named
weight here rather than an implicit 1/N default buried inside the index
math itself — so the actual weighting scheme is one small file a reviewer
(or DGCA) can read and audit, and the one file to edit when real
route-level passenger-traffic-share data becomes available to replace it.

DEFAULT_ROUTE_WEIGHTS below is EQUAL-WEIGHT across every route in the
basket — the only defensible default without real traffic-share data, and
explicitly a placeholder: swap the values here for DGCA-published
route-level passenger volumes (or another traffic proxy) once available.
Nothing in app/index/psd_index.py hardcodes equal weighting — it only ever
reads through route_weight()/normalized_weights(), so changing the numbers
here is the entire job.
"""

from __future__ import annotations

from app.ingestion.scheduler import ROUTE_PAIRS

DEFAULT_ROUTE_WEIGHTS: dict[str, float] = {route.route_id: 1.0 for route in ROUTE_PAIRS}


def route_weight(route_id: str, weights: dict[str, float] | None = None) -> float:
    """A single route's configured weight, or 0.0 if it isn't in the
    table at all (an off-basket route should never contribute)."""
    table = weights if weights is not None else DEFAULT_ROUTE_WEIGHTS
    return table.get(route_id, 0.0)


def normalized_weights(
    route_ids: list[str], weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """Weights for exactly `route_ids`, renormalized to sum to 1 across
    just that set.

    This renormalization is what keeps a day with a missing route's data
    from silently deflating the index: if a route drops out (no quotes
    collected that day), its weight is simply redistributed across the
    routes that DID report, rather than vanishing from the numerator while
    staying in some fixed denominator.
    """
    table = weights if weights is not None else DEFAULT_ROUTE_WEIGHTS
    raw = {rid: table.get(rid, 0.0) for rid in route_ids}
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {rid: w / total for rid, w in raw.items()}
