"""Validate every scheduled opportunity and estimate paired, session-clustered uncertainty."""

import argparse
from collections import Counter
import gzip
import hashlib
import json
import math
import statistics
from pathlib import Path


def critical(df):
    # Integral of cos(theta)^(df-1) after substituting x=sqrt(df)*tan(theta)
    norm = math.exp(math.lgamma((df + 1) / 2) - math.lgamma(df / 2)) / math.sqrt(
        math.pi
    )

    def cdf(x):
        theta = math.atan(x / math.sqrt(df))
        c = math.cos(theta)
        s = math.sin(theta)
        n = df - 1
        value = theta if n % 2 == 0 else s
        for m in range(2 + (n % 2), n + 1, 2):
            value = (s * c ** (m - 1) + (m - 1) * value) / m
        return 0.5 + norm * value

    low, high = 0.0, 16.0
    for _ in range(52):
        mid = (low + high) / 2
        if cdf(mid) < 0.975:
            low = mid
        else:
            high = mid
    return high


def count_session(d):
    if not d["complete"] or len(d["rows"]) != len(d["planned"]):
        raise ValueError(f"incomplete session {d['seed']}")
    totals = {typ: [Counter(), Counter()] for typ in ("witnessed/full", "full/full")}
    for p, r in zip(d["planned"], d["rows"]):
        assert (
            p["index"] == r["index"]
            and p["arm"] == r["arm"]
            and len(r["producers"]) == 2
        )
        selected = r["selected_ancestry"]
        assert len(selected) == 2 and len(set(selected)) == 1
        assert (
            len(r["session_final_ancestry"]) == 2
            and len(set(r["session_final_ancestry"])) == 1
        )
        counts = totals[r["arm"]]
        for arm, c in zip(r["producers"], counts):
            c["scheduled"] += 1
            c["ready"] += arm["ready"]
            c["hashes"] += arm["hashes"]
            assert (arm["hashes"] > 0) == arm["ready"]
            sol = arm["discovery"]
            if not sol:
                continue
            body = bytes.fromhex(sol["wire_hex"])
            header = body[:80]
            h = hashlib.sha256(hashlib.sha256(header).digest()).digest()
            assert (
                h[::-1].hex() == sol["hash"]
                and int.from_bytes(h, "little") <= 0x7FFFFF << 232
            )
            assert header[4:36][::-1].hex() == r["parent"] and sol["settled"] == (
                selected[0] == sol["hash"]
            )
            c["discovered"] += 1
            c["stale"] += not sol["settled"]
            c["settled"] += sol["settled"]
            c["later_detached"] += (
                sol["settled"] and sol["hash"] != r["session_final_ancestry"][0]
            )
    return totals


def estimate(sessions, arm):
    n = len(sessions)
    pooled = [sum((s[arm][i] for s in sessions), Counter()) for i in range(2)]
    rates = [c["stale"] / c["discovered"] for c in pooled]
    influence = []
    for s in sessions:
        a, b = [
            (s[arm][i]["stale"] - rates[i] * s[arm][i]["discovered"])
            / (pooled[i]["discovered"] / n)
            for i in range(2)
        ]
        influence.append(a - b)
    difference = rates[0] - rates[1]
    sd = statistics.stdev(influence)
    half = critical(n - 1) * sd / math.sqrt(n)
    return {
        "roles": pooled,
        "stale_rates": rates,
        "stale_difference": difference,
        "session_influence_sd": sd,
        "confidence_interval_95": [
            max(-1.0, difference - half),
            min(1.0, difference + half),
        ],
        "half_width": half,
        "achieved_one_point_precision": half <= 0.01,
    }


def read(path):
    with gzip.open(path, "rt") if path.suffix == ".gz" else path.open() as f:
        return json.load(f)


def analyze(directory, phase, target=None):
    files = sorted((*directory.glob("*.json"), *directory.glob("*.json.gz")))
    records = [read(p) for p in files]
    start = 41001 if phase == "pilot" else 51001
    n = 16 if phase == "pilot" else target
    if n is None or n < 16:
        raise ValueError("evaluation requires the frozen pilot session count")
    if {r["seed"] for r in records} != set(range(start, start + n)) or len(
        records
    ) != n:
        raise ValueError("wrong cohort or missing sessions")
    if len({r["binary_sha256"] for r in records}) != 1:
        raise ValueError("binary changed during the cohort")
    for r in records:
        assert len(r["planned"]) == 32 and all(
            x["offer_delay_ms"] == 1200 for x in r["planned"]
        )
        assert Counter(x["arm"] for x in r["planned"]) == {
            "witnessed/full": 16,
            "full/full": 16,
        }
    counts = [count_session(d) for d in records]
    arms = {a: estimate(counts, a) for a in ("witnessed/full", "full/full")}
    summary = {
        "phase": phase,
        "sessions": n,
        "binary_sha256": records[0]["binary_sha256"],
        "arms": arms,
    }
    if phase == "pilot":
        sd = arms["witnessed/full"]["session_influence_sd"]
        summary["fresh_evaluation_sessions"] = max(
            16, math.ceil((critical(15) * sd / 0.01) ** 2)
        )
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--phase", choices=["pilot", "evaluation"], required=True)
    p.add_argument("--pilot-summary", type=Path)
    a = p.parse_args()
    t = read(a.pilot_summary)["fresh_evaluation_sessions"] if a.pilot_summary else None
    print(json.dumps(analyze(a.directory, a.phase, t), indent=2))
