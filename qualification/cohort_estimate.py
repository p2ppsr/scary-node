"""Validate every scheduled opportunity and estimate paired, session-clustered uncertainty."""

import argparse
from collections import Counter
import gzip
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import decode_block, dsha, txid

STUDIES = {
    "original": {"pilot": 41001, "evaluation": 51001},
    "immediate": {"pilot": 71001, "evaluation": 81001},
}


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
    for index, (p, r) in enumerate(zip(d["planned"], d["rows"])):
        if (
            p["index"] != index
            or r["index"] != index
            or p["arm"] != r["arm"]
            or len(r["producers"]) != 2
        ):
            raise ValueError("session row differs from its scheduled opportunity")
        if index:
            prev = d["rows"][index - 1]
            if (
                r["parent"] != prev["settlement_tip"]
                or r["height"] != prev["height"] + 3
            ):
                raise ValueError("opportunity does not follow the prior settled parent")
        selected = r["selected_ancestry"]
        for ancestry in (selected, r["session_final_ancestry"]):
            if len(ancestry) != 2 or len(set(ancestry)) != 1:
                raise ValueError(
                    "independent receiver ancestry missing or inconsistent"
                )
        counts = totals[r["arm"]]
        roles = (
            "witnessed" if r["arm"] == "witnessed/full" else "full-left",
            "full-right",
        )
        if tuple(a["role"] for a in r["producers"]) != roles:
            raise ValueError("unexpected producer order")
        attempts = [a["hashes"] for a in r["producers"] if a["ready"]]
        if len(set(attempts)) > 1 or any(a < 1 or a > 64 for a in attempts):
            raise ValueError("unequal or out-of-range hash attempts on ready lanes")
        for i, (arm, c) in enumerate(zip(r["producers"], counts)):
            c["scheduled"] += 1
            c["ready"] += arm["ready"]
            c["hashes"] += arm["hashes"]
            if (arm["hashes"] > 0) != arm["ready"]:
                raise ValueError("hash effort disagrees with readiness")
            sol = arm["discovery"]
            if not sol:
                continue
            body = bytes.fromhex(sol["wire_hex"])
            header = body[:80]
            h = dsha(header)
            _, transactions, _ = decode_block(body, sol["hash"])
            if len(transactions) != 2 or txid(transactions[1]) != r["payment_txid"]:
                raise ValueError("competitive block lacks the exact common payment")
            if not arm["ready"] or arm["native_submit"] is None:
                raise ValueError(
                    "discovery missing ready job or publication disposition"
                )
            expected_nonce = (
                p["nonce"] + 2 * (arm["hashes"] - 1) + (i ^ int(p["swap_nonce"]))
            )
            if (
                int.from_bytes(header[72:76], "little") != 0x207FFFFF
                or int.from_bytes(header[76:80], "little") != expected_nonce
                or sol["nonce"] != expected_nonce
            ):
                raise ValueError(
                    "competitive header differs from assigned target or nonce lane"
                )
            if int.from_bytes(h, "little") > 0x7FFFFF << 232:
                raise ValueError("discovery misses regtest proof-of-work target")
            if header[4:36][::-1].hex() != r["parent"] or sol["settled"] != (
                selected[0] == sol["hash"]
            ):
                raise ValueError(
                    "discovery disagrees with parent or independent settlement"
                )
            c["discovered"] += 1
            c["stale"] += not sol["settled"]
            c["settled"] += sol["settled"]
            c["later_detached"] += (
                sol["settled"] and sol["hash"] != r["session_final_ancestry"][0]
            )
    return totals


def ratio_influences(sessions, arm, numerator, denominator):
    n = len(sessions)
    pooled = [sum((s[arm][i] for s in sessions), Counter()) for i in range(2)]
    rates = [c[numerator] / c[denominator] for c in pooled]
    influence = []
    for s in sessions:
        a, b = [
            (s[arm][i][numerator] - rates[i] * s[arm][i][denominator])
            / (pooled[i][denominator] / n)
            for i in range(2)
        ]
        influence.append(a - b)
    return pooled, rates, influence


def interval(difference, influence, bound=1.0):
    n = len(influence)
    sd = statistics.stdev(influence)
    half = critical(n - 1) * sd / math.sqrt(n)
    return {
        "session_influence_sd": sd,
        "confidence_interval_95": [
            max(-bound, difference - half),
            min(bound, difference + half),
        ],
        "half_width": half,
    }


def estimate(sessions, arm):
    pooled, rates, influence = ratio_influences(sessions, arm, "stale", "discovered")
    difference = rates[0] - rates[1]
    limits = interval(difference, influence)
    return {
        "roles": pooled,
        "stale_rates": rates,
        "stale_difference": difference,
        **limits,
        "achieved_one_point_precision": limits["half_width"] <= 0.01,
        "per_scheduled_opportunity": {
            event: [c[event] / c["scheduled"] for c in pooled]
            for event in ("ready", "discovered", "settled")
        },
    }


def read(path):
    with gzip.open(path, "rt") if path.suffix == ".gz" else path.open() as f:
        return json.load(f)


def analyze(directory, phase, target=None, study="original", expected_binary=None):
    files = sorted((*directory.glob("*.json"), *directory.glob("*.json.gz")))
    start = STUDIES[study][phase]
    n = 16 if phase == "pilot" else target
    if n is None or n < 16:
        raise ValueError("evaluation requires the frozen pilot session count")
    if len(files) != n:
        raise ValueError("wrong cohort or missing sessions")
    seeds, hashes, counts = set(), set(), []
    availability = {}
    for path in files:
        r = read(path)
        seeds.add(r["seed"])
        hashes.add(r["binary_sha256"])
        if (
            len(r["planned"]) != 32
            or any(x["offer_delay_ms"] != 1200 for x in r["planned"])
            or Counter(x["arm"] for x in r["planned"])
            != {"witnessed/full": 16, "full/full": 16}
        ):
            raise ValueError("session schedule differs from registered design")
        counts.append(count_session(r))
        for row in r["rows"]:
            period = "first_four_parents" if row["index"] < 4 else "remaining_parents"
            for producer in row["producers"]:
                measures = availability.setdefault(period, {}).setdefault(
                    row["arm"], {}
                )
                role = measures.setdefault(producer["role"], Counter())
                role["scheduled"] += 1
                role["ready"] += producer["ready"]
                if not producer["ready"]:
                    role[producer["readiness_reason"]] += 1
    if seeds != set(range(start, start + n)):
        raise ValueError("wrong cohort or missing sessions")
    if len(hashes) != 1:
        raise ValueError("binary changed during the cohort")
    if expected_binary is not None and hashes != {expected_binary}:
        raise ValueError("cohort executable differs from frozen study binary")
    arms = {a: estimate(counts, a) for a in ("witnessed/full", "full/full")}
    summary = {
        "phase": phase,
        "study": study,
        "availability_by_parent": availability,
        "sessions": n,
        "binary_sha256": hashes.pop(),
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
    p.add_argument("--study", choices=STUDIES, default="original")
    p.add_argument("--pilot-summary", type=Path)
    a = p.parse_args()
    pilot = read(a.pilot_summary) if a.pilot_summary else {}
    if pilot and (
        pilot.get("study", "original") != a.study or pilot["phase"] != "pilot"
    ):
        p.error("the sizing summary must belong to this study")
    print(
        json.dumps(
            analyze(
                a.directory,
                a.phase,
                pilot.get("fresh_evaluation_sessions"),
                a.study,
                pilot.get("binary_sha256"),
            ),
            indent=2,
        )
    )
