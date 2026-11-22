"""Measure successor-triggered admission by original intake route and sampled time interval."""

import argparse
import bisect
import json
from itertools import pairwise
from pathlib import Path

from recover_sustained_chain import read
from verify_sustained_native import require, verify

COUNTERS = (
    "awaiting_successor",
    "promoted_after_successor",
    "discarded_after_recheck",
    "admitted",
    "duplicate",
    "ineligible",
)
ROUTES = ("sv", "teranode_subtree")
PERIOD = 300_000_000_000


def measure(samples):
    observations = []
    for sample in samples:
        entrant = sample.get("scary", {})
        routes = entrant.get("peer_intake", {})
        if not all("awaiting_successor" in routes.get(route, {}) for route in ROUTES):
            continue
        require(
            all(
                all(
                    isinstance(routes[route].get(key), int) and routes[route][key] >= 0
                    for key in COUNTERS
                )
                for route in ROUTES
            ),
            "incomplete successor intake counters",
        )
        observations.append(entrant)
    if not observations:
        return {"observations": 0, "intervals": []}
    stamps = [obs["read_ns"] for obs in observations]
    require(
        stamps == sorted(stamps), "entrant counter observation clock moved backwards"
    )
    cuts = [
        (bisect.bisect_right(stamps, end) - 1, end)
        for end in range(PERIOD, 6 * PERIOD + 1, PERIOD)
    ]
    cuts.append((len(stamps) - 1, None))
    start = 0
    intervals = []
    for i, (end, scheduled_end) in enumerate(cuts):
        observed = observations[start : end + 1]
        deltas = {}
        resets = []
        if len(observed) > 1:
            for route in ROUTES:
                deltas[route] = {}
                for key in COUNTERS:
                    counters = [obs["peer_intake"][route][key] for obs in observed]
                    decreased = any(b < a for a, b in pairwise(counters))
                    if decreased:
                        resets.append([route, key])
                    deltas[route][key] = (
                        None if decreased else counters[-1] - counters[0]
                    )
        intervals.append(
            {
                "phase": "offer" if i < 6 else "drain",
                "nominal_start_ns": i * PERIOD,
                "nominal_end_ns": scheduled_end,
                "first_entrant_read_ns": stamps[start] if observed else None,
                "last_entrant_read_ns": stamps[end] if observed else None,
                "endpoint_heights": [observed[0]["height"], observed[-1]["height"]]
                if observed
                else [],
                "new_observations": max(0, len(observed) - 1),
                "counter_decreases": resets,
                "changes": deltas,
            }
        )
        start = max(start, end)
    return {"observations": len(observations), "intervals": intervals}


def analyze(record):
    summary = verify(record)
    result = measure(record["samples"])
    if result["observations"] >= 2 and not any(
        row["counter_decreases"] for row in result["intervals"]
    ):
        totals = {
            route: {
                key: sum(
                    row["changes"].get(route, {}).get(key, 0)
                    for row in result["intervals"]
                )
                for key in COUNTERS
            }
            for route in ROUTES
        }
        expected = summary["sampled_peer_intake_change"]
        require(
            all(
                expected[route][key] == totals[route][key]
                for route in ROUTES
                for key in COUNTERS
            ),
            "intervals do not reproduce the original sampled totals",
        )
        result["full_sampled_changes"] = totals
    return {"arm": record.get("arm"), "registration": record["registration"], **result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    print(json.dumps(analyze(read(parser.parse_args().receipt)), indent=2))
