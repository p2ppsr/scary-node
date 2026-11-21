"""Tabulate competing/sole native discoveries and observed latency; compare immediate and timed publication."""

import argparse
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path
import statistics
from cohort_estimate import count_session, estimate, interval, ratio_influences, read


def diagnosis(records):
    results = {}
    counts_by_session = [count_session(r) for r in records]
    hashes = {r["binary_sha256"] for r in records}
    if len(hashes) != 1:
        raise ValueError("a comparison arm must use one measured executable")
    for typ in ("witnessed/full", "full/full"):
        counts = [Counter(), Counter()]
        latency = [defaultdict(list), defaultdict(list)]
        since_submit = [defaultdict(list), defaultdict(list)]
        ack_time = [defaultdict(list), defaultdict(list)]
        for r in records:
            for row in r["rows"]:
                if row["arm"] != typ:
                    continue
                producers = row["producers"]
                both = all(p["discovery"] for p in producers)
                for i, p in enumerate(producers):
                    c = counts[i]
                    c["scheduled"] += 1
                    c["unready"] += not p["ready"]
                    sol = p["discovery"]
                    if not sol:
                        continue
                    cat = "tie" if both else "solo"
                    c[cat + "_discoveries"] += 1
                    c[cat + "_settled"] += sol["settled"]
                    submitted = p["native_submit"]
                    if "ack" in submitted:
                        ack_time[i][cat].append(
                            (submitted["finish_ns"] - submitted["start_ns"]) / 1e6
                        )
                    if "error" in submitted:
                        c["submit_errors"] += 1
                    observations = [
                        m[sol["hash"]]
                        for m in row["receiver_first_active_ns"]
                        if sol["hash"] in m
                    ]
                    if observations:
                        first = min(observations)
                        if first < submitted["start_ns"]:
                            raise ValueError(
                                "receiver observation predates native submission"
                            )
                        latency[i][cat].append(first / 1e6)
                        since_submit[i][cat].append(
                            (first - submitted["start_ns"]) / 1e6
                        )
        results[typ] = [
            {
                "counts": dict(c),
                "first_active_median_ms": {
                    k: statistics.median(v) for k, v in lat.items()
                },
                "first_active_observations": {k: len(v) for k, v in lat.items()},
                "submission_to_first_active_median_ms": {
                    k: statistics.median(v) for k, v in elapsed.items()
                },
                "successful_native_ack_median_ms": {
                    k: statistics.median(v) for k, v in ack.items()
                },
            }
            for c, lat, elapsed, ack in zip(counts, latency, since_submit, ack_time)
        ]
    return {
        "sessions": len(records),
        "binary_sha256": hashes.pop(),
        "arms": results,
        "estimates": {arm: estimate(counts_by_session, arm) for arm in results},
    }


def receipts(directory):
    return [read(p) for p in sorted(directory.glob("*.json.gz"))]


def paired_report(directory):
    raw = {mode: receipts(directory / mode) for mode in ("timer", "immediate")}
    counts = {}
    for mode, records in raw.items():
        if [r["seed"] for r in records] != list(range(62001, 62005)):
            raise ValueError("the four preregistered seed pairs must be complete")
        counts[mode] = [count_session(r) for r in records]
    for timer, immediate in zip(raw["timer"], raw["immediate"]):
        if (timer["planned"], timer["physical_swap"], timer["sv_image"]) != (
            immediate["planned"],
            immediate["physical_swap"],
            immediate["sv_image"],
        ):
            raise ValueError(
                "paired scheduling, orientation or reference build differs"
            )
    report = {mode: diagnosis(records) for mode, records in raw.items()}
    if report["timer"]["binary_sha256"] == report["immediate"]["binary_sha256"]:
        raise ValueError("both modes used the same executable")
    contrasts = {}
    for arm in ("witnessed/full", "full/full"):
        _, first_rates, first = ratio_influences(
            counts["timer"], arm, "stale", "discovered"
        )
        _, second_rates, second = ratio_influences(
            counts["immediate"], arm, "stale", "discovered"
        )
        delta = (second_rates[0] - second_rates[1]) - (first_rates[0] - first_rates[1])
        contrasts[arm] = {
            "difference_of_stale_differences": delta,
            **interval(delta, [b - a for a, b in zip(first, second)], bound=2.0),
        }
    report["immediate_minus_timer"] = contrasts
    return report


def compare(directory, timer, immediate):
    from native_cohort import run
    from run_cohort import session_lock

    directory.mkdir(parents=True, exist_ok=True)
    binaries = {"timer": timer, "immediate": immediate}
    with session_lock():
        for i, seed in enumerate(range(62001, 62005)):
            order = ("timer", "immediate") if i % 2 == 0 else ("immediate", "timer")
            for mode in order:
                out = directory / mode
                out.mkdir(exist_ok=True)
                target = out / f"{seed}.json.gz"
                if target.exists():
                    count_session(read(target))
                    continue
                temp = out / f"{seed}.partial"
                run(temp, binaries[mode], seed, 32, 1200)
                count_session(read(temp))
                target.write_bytes(gzip.compress(temp.read_bytes(), mtime=0))
                temp.unlink()
    return paired_report(directory)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument(
        "--paired", action="store_true", help="replay a completed comparison"
    )
    p.add_argument("--timer", type=Path)
    p.add_argument("--immediate", type=Path)
    a = p.parse_args()
    if bool(a.timer) != bool(a.immediate):
        p.error("supply both builds for a paired comparison")
    report = (
        compare(a.directory, a.timer, a.immediate)
        if a.timer
        else (
            paired_report(a.directory) if a.paired else diagnosis(receipts(a.directory))
        )
    )
    print(json.dumps(report, indent=2))
