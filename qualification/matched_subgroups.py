"""Replay the prespecified descriptive receiver-certified subgroups only after an entire phase closes."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from cohort_archive import canonical, check, contrast
from cohort_estimate import read
from matched_archive import sessions
from matched_study import ARMS, close_phase, phase_bounds

GROUPS = ("all_assigned", "all_ties", "both_receivers_certified_ties", "other_ties")


def group_counts():
    return {
        "parents": 0,
        "lane": [Counter(), Counter()],
        "discovery_patterns": Counter(),
        "selected_at_depth_two": Counter(),
        "receivers": [Counter(), Counter()],
    }


def classify(row):
    lanes = row["lanes"]
    found = [p["discovery"] is not None for p in lanes]
    if not all(found):
        return (
            ("left_only" if found[0] else "right_only") if any(found) else "neither"
        ), ["all_assigned"]
    certified = all(s["status"] == "certified" for s in row["ingress_alignment"])
    return "tie", [
        "all_assigned",
        "all_ties",
        "both_receivers_certified_ties" if certified else "other_ties",
    ]


def record(row, count):
    pattern, groups = classify(row)
    winner = row["settlement"]["depth_two_ancestry"][0]
    picked = "continuation_only"
    for i, lane in enumerate(row["lanes"]):
        if lane["discovery"] and lane["discovery"]["hash"] == winner:
            picked = ("left", "right")[i]
    for g in groups:
        x = count[g]
        x["parents"] += 1
        x["discovery_patterns"][pattern] += 1
        x["selected_at_depth_two"][picked] += 1
        for lane, role in zip(row["lanes"], x["lane"]):
            role.update(scheduled=1, ready=int(lane["ready"]), hashes=lane["hashes"])
            found = lane["discovery"]
            if found is None:
                continue
            won = winner == found["hash"]
            role.update(
                discovered=1,
                stale=int(not won),
                settled=int(won),
                present_at_session_end=int(
                    row["session_final_ancestry"][0] == found["hash"]
                ),
                native_body_available=int("native_wire_hex" in lane),
                native_body_missing=int("native_wire_hex" not in lane),
                native_captured_after_common_cutoff=int(
                    lane.get("native_capture_ns", 0) > row["hold_ns"]
                ),
            )
        for i, rx in enumerate(x["receivers"]):
            rx["assigned_parents"] += 1
            rx["ingress_" + row["ingress_alignment"][i]["status"]] += 1
            rx["transport_certified"] += int(
                row["ingress_alignment"][i]["status"] == "certified"
            )
            for key in ("predeadline", "postdeadline"):
                site = row[key + "_readbacks"][i]
                rx[key + "_active_lookups"] += 1
                rx[key + "_active_unknown"] += int(site["active_hash"] is None)
                for body in site["bodies"].values():
                    if "body_sha256" in body:
                        disposition = "body_verified"
                    elif body.get("rpc_code") in (-1, -5):
                        disposition = "stock_reported_unavailable"
                    elif "rpc_code" in body:
                        disposition = "other_stock_rpc_error"
                    else:
                        disposition = "transport_exhausted"
                    rx[key + "_by_hash_lookups"] += 1
                    rx[key + "_" + disposition] += 1
                    rx[key + "_by_hash_retries"] += len(body["attempts"]) - 1


def combine(grouped):
    out = group_counts()
    for entry in grouped:
        out["parents"] += entry["parents"]
        for name in ("discovery_patterns", "selected_at_depth_two"):
            out[name].update(entry[name])
        for name in ("lane", "receivers"):
            for to, source in zip(out[name], entry[name]):
                to.update(source)
    return out


def analyze(directory, registration, pilot=None):
    # Verify complete fixed-N enrollment and replay the primary analysis before reading subgroups.
    check((directory / "summary.json").is_file(), "the registered phase has not closed")
    reg = read(registration)
    claimed = read(directory / "summary.json")
    phase = claimed["phase"]
    size = read(pilot) if pilot else None
    primary = close_phase(directory, reg, phase, size)
    check(
        primary == claimed,
        "archive differs from its independent complete fixed-phase replay",
    )
    d = reg["design"]
    start, n = phase_bounds(d, phase, size)
    offsets = (-d["signed_delay_ns"], 0, d["signed_delay_ns"])
    labels = [f"{arm}:{ns}ns" for arm in ARMS for ns in offsets]
    data = []
    for _, rec, _ in sessions(directory, [f"{start + i}.json.gz" for i in range(n)]):
        session = {k: {g: group_counts() for g in GROUPS} for k in labels}
        for row in rec["rows"]:
            key = (
                f"{row['plan']['arm']}:{row['offsets_ns'][0] - row['offsets_ns'][1]}ns"
            )
            record(row, session[key])
        data.append(session)
    result = {}
    for key in labels:
        treatment = {}
        for g in GROUPS:
            group = combine([s[key][g] for s in data])
            rates = [{g: s[key][g]["lane"]} for s in data]
            group["stale_per_discovery_left_minus_right"] = contrast(
                rates, g, "stale", "discovered"
            )
            treatment[g] = group
        all_assigned = treatment["all_assigned"]
        check(
            all_assigned["parents"] == primary["pooled"][key]["parents"]
            and all(
                all_assigned["lane"][i]["stale"]
                == primary["pooled"][key]["lanes"][i]["stale"]
                for i in range(2)
            ),
            "assigned subgroup differs from the registered primary analysis",
        )
        check(
            treatment["all_ties"]["parents"]
            == treatment["both_receivers_certified_ties"]["parents"]
            + treatment["other_ties"]["parents"],
            "unaccounted tie",
        )
        result[key] = treatment
    return {
        "scope": "Descriptive, conditioned on observed ties and receiver certification; each parent is counted once per applicable subgroup.",
        "reference": "The registered all-assigned result and pilot-derived sample size are independent of these subgroups.",
        "phase": phase,
        "complete_independent_sessions": n,
        "scheduled_parents": primary["parents"],
        "primary_summary_sha256": hashlib.sha256(canonical(claimed)).hexdigest(),
        "by_role_and_assigned_offset": result,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive", type=Path)
    p.add_argument("registration", type=Path)
    p.add_argument("--pilot-summary", type=Path)
    args = p.parse_args()
    print(
        json.dumps(
            analyze(args.archive, args.registration, args.pilot_summary), indent=2
        )
    )
