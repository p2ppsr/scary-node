"""Compare worker, local publication and stock-tip observations for the same regtest block."""

import argparse
import collections
import datetime
import gzip
import json
from pathlib import Path

from verify_sustained_native import distribution, require, sha256_hex


def load(path):
    read = gzip.open if path.suffix == ".gz" else open
    with read(path, "rt") as handle:
        return json.load(handle)


def docker_events(lines):
    events = []
    for line in lines:
        stamp, payload = line.rstrip().split(" ", 1)
        row = json.loads(payload)
        require(
            row.get("event") == "gossip_published" and sha256_hex(row["hash"]),
            "unexpected publication log entry",
        )
        seconds, nanos = stamp.removesuffix("Z").split(".")
        require(
            stamp.endswith("Z") and len(nanos) == 9 and nanos.isdigit(),
            "expected UTC Docker timestamp with nanoseconds",
        )
        epoch = datetime.datetime.fromisoformat(seconds).replace(tzinfo=datetime.UTC)
        events.append((int(epoch.timestamp()) * 10**9 + int(nanos), row["hash"]))
    return events


def analyze(record, events, ledger):
    from workload_solved_ledger import audit as audit_ledger

    ownership = audit_ledger(record, ledger)
    selected = {b["hash"]: b for b in record["blocks"]}
    local = {s["hash"]: s for s in ledger["records"]}
    by_hash = collections.defaultdict(list)
    for stamp, digest in events:
        require(digest in local, "publication event absent from local solution journal")
        by_hash[digest].append(stamp)
    acknowledged = {
        s["hash"]: s for s in record["slots"] if s["miner"] == "scary" and s.get("hash")
    }
    slots = {(r["phase"], r["index"]): r for r in record["slots"]}
    linked = {
        b["hash"]: slots[(b["worker_phase"], b["worker_index"])]
        for b in ownership["selected"]
        if b["owner"] == "scary" and b["worker_phase"] is not None
    }
    rows = []
    for digest, state in local.items():
        slot = acknowledged.get(digest) or linked.get(digest)
        times = sorted(by_hash[digest])
        body = selected.get(digest)
        logged_ns = times[0] if times else None
        queued = state["queued_at_ms"]
        marked = state["published_at_ms"]
        end = record["start_wall_ns"] + slot["finished_ns"] if slot else None
        directions = collections.defaultdict(list)
        for peer, reports in state["deliveries"].items():
            if peer.startswith("sv-peer-"):
                for direction, at_ms in reports.items():
                    directions[direction].append(at_ms)
        rows.append(
            {
                "hash": digest,
                "height": state["height"],
                "selected": body is not None,
                "worker_phase": slot["phase"] if slot else None,
                "worker_index": slot["index"] if slot else None,
                "worker_outcome": slot["outcome"] if slot else None,
                "worker_slot_elapsed_ms": (slot["finished_ns"] - slot["started_ns"])
                / 1e6
                if slot
                else None,
                "local_queue_to_positive_receipt_ms": marked - queued
                if marked is not None
                else None,
                "positive_receipt_to_first_docker_log_ms": (logged_ns - marked * 10**6)
                / 1e6
                if logged_ns is not None and marked is not None
                else None,
                "positive_log_minus_estimated_worker_end_ms": (logged_ns - end) / 1e6
                if logged_ns is not None and end is not None
                else None,
                "stock_tip_sample_minus_worker_end_ms": {
                    role: (t - slot["finished_ns"]) / 1e6
                    if slot and t is not None
                    else None
                    for role, t in body["first_observed_ns"].items()
                    if role != "scary"
                }
                if body
                else None,
                "local_queue_to_first_sv_delivery_receipt_ms": {
                    direction: min(stamps) - queued
                    for direction, stamps in directions.items()
                },
                "local_queue_to_first_inclusion_observation_ms": state[
                    "first_inclusion_at_ms"
                ]
                - queued
                if state["first_inclusion_at_ms"] is not None
                else None,
                "log_entries": len(times),
            }
        )
    summary = {}
    for name, group in (
        (
            "acknowledged_primary_selected",
            [
                r
                for r in rows
                if r["selected"]
                and r["worker_outcome"] == "solved"
                and r["worker_phase"] == "offer"
            ],
        ),
        (
            "acknowledged_primary_outside_branch",
            [
                r
                for r in rows
                if not r["selected"]
                and r["worker_outcome"] == "solved"
                and r["worker_phase"] == "offer"
            ],
        ),
        (
            "selected_after_worker_error",
            [r for r in rows if r["selected"] and r["worker_outcome"] == "error"],
        ),
        ("all_local", rows),
    ):
        stats = {
            "solutions": len(group),
            "with_positive_log": sum(bool(r["log_entries"]) for r in group),
        }
        for key in (
            "worker_slot_elapsed_ms",
            "local_queue_to_positive_receipt_ms",
            "local_queue_to_first_inclusion_observation_ms",
            "positive_receipt_to_first_docker_log_ms",
            "positive_log_minus_estimated_worker_end_ms",
        ):
            stats[key] = distribution([r[key] for r in group if r[key] is not None])
        directions = sorted(
            {
                key
                for r in group
                for key in r["local_queue_to_first_sv_delivery_receipt_ms"]
            }
        )
        stats["local_queue_to_first_sv_delivery_receipt_ms"] = {
            key: distribution(
                [
                    r["local_queue_to_first_sv_delivery_receipt_ms"][key]
                    for r in group
                    if key in r["local_queue_to_first_sv_delivery_receipt_ms"]
                ]
            )
            for key in directions
        }
        summary[name] = stats
    return {"total_log_entries": len(events), "groups": summary, "solutions": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("publication_log", type=Path)
    parser.add_argument("solved_ledger", type=Path)
    args = parser.parse_args()
    with args.publication_log.open() as source:
        print(
            json.dumps(
                analyze(
                    load(args.receipt), docker_events(source), load(args.solved_ledger)
                ),
                indent=2,
            )
        )
