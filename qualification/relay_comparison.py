"""Tabulate competing/sole native discoveries and observed latency; compare immediate and timed publication."""

import argparse
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path
import statistics
from cohort_estimate import count_session, read
from native_cohort import run
from run_cohort import session_lock


def diagnosis(directory):
    results = {}
    records = [read(p) for p in sorted(directory.glob("*.json.gz"))]
    for r in records:
        count_session(r)
    for typ in ("witnessed/full", "full/full"):
        counts = [Counter(), Counter()]
        latency = [defaultdict(list), defaultdict(list)]
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
                    if "error" in p["native_submit"]:
                        c["submit_errors"] += 1
                    observations = [
                        m[sol["hash"]]
                        for m in row["receiver_first_active_ns"]
                        if sol["hash"] in m
                    ]
                    if observations:
                        latency[i][cat].append(min(observations) / 1e6)
        results[typ] = [
            {
                "counts": dict(c),
                "first_active_median_ms": {
                    k: statistics.median(v) for k, v in lat.items()
                },
                "first_active_observations": {k: len(v) for k, v in lat.items()},
            }
            for c, lat in zip(counts, latency)
        ]
    return {"sessions": len(records), "arms": results}


def compare(directory, timer, immediate):
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
    return {m: diagnosis(directory / m) for m in binaries}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--timer", type=Path)
    p.add_argument("--immediate", type=Path)
    a = p.parse_args()
    if bool(a.timer) != bool(a.immediate):
        p.error("supply both builds for a paired comparison")
    report = (
        compare(a.directory, a.timer, a.immediate)
        if a.timer
        else diagnosis(a.directory)
    )
    print(json.dumps(report, indent=2))
