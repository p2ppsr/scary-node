"""Match a workload worker's exact prepared proof of work, including missing replies."""

import argparse
import gzip
import json
import struct
from collections import Counter, defaultdict
from pathlib import Path

from verify_native_workers import dsha, previous_hash
from verify_receipt import check_header, require


def prepared_header(row):
    candidate = row["worker_candidate"]
    job = candidate["notify"]
    require(
        isinstance(job, list) and len(job) == 9 and isinstance(job[0], str) and job[0],
        "missing Stratum job",
    )
    header = bytes.fromhex(candidate["header"])
    check_header(header, candidate["hash"], row["parent"])
    coinbase = bytes.fromhex(
        job[2] + candidate["extranonce1"] + candidate["extranonce2"] + job[3]
    )
    root = dsha(coinbase)
    for sibling in job[4]:
        branch = bytes.fromhex(sibling)
        require(len(branch) == 32, "invalid Merkle sibling")
        root = dsha(root + branch)
    prefix = (
        struct.pack("<I", int(job[5], 16))
        + previous_hash(job)
        + root
        + struct.pack("<II", int(job[7], 16), int(job[6], 16))
    )
    require(header[:76] == prefix, "worker header differs from its Stratum job")
    if "solution_header" in row:
        require(
            row["solution_header"] == candidate["header"], "response header differs"
        )
    if row["outcome"] == "solved":
        require(row["hash"] == candidate["hash"], "acknowledged solution hash differs")
    return candidate


def audit(record):
    require(
        record.get("worker_header_format") == 1,
        "receipt predates prepared-header capture",
    )
    attempts = defaultdict(list)
    wire_headers = {}
    dispositions = Counter()
    without_header = Counter()
    for row in record["slots"]:
        if row["miner"] != "scary":
            require(
                "worker_candidate" not in row, "Stratum proof attributed to stock miner"
            )
            continue
        outcome = row["outcome"]
        if "worker_candidate" not in row:
            require(
                outcome in ("error", "unready", "missed"),
                "worker response missing its prepared header",
            )
            without_header[outcome] += 1
            continue
        require(
            row["ready"] and outcome in ("solved", "error", "rejected"),
            "unready worker supplied a prepared header",
        )
        candidate = prepared_header(row)
        header = bytes.fromhex(candidate["header"])
        require(
            wire_headers.setdefault(candidate["hash"], header) == header,
            "different prepared bytes for one hash",
        )
        attempts[candidate["hash"]].append(
            {
                "phase": row["phase"],
                "index": row["index"],
                "job_id": candidate["notify"][0],
                "worker_outcome": outcome,
            }
        )
        dispositions[outcome] += 1

    selected = []
    for body in record["blocks"]:
        matches = attempts.get(body["hash"])
        if matches:
            header = bytes.fromhex(body["header_hex"])
            require(
                wire_headers[body["hash"]] == header,
                "selected and prepared header bytes differ",
            )
            check_header(header, body["hash"])
            selected.append(
                {
                    "hash": body["hash"],
                    "height": body["height"],
                    "payments": body["scheduled_payments"],
                    "attempts": matches,
                }
            )
    primary_payments = 0
    intervals = [{"selected_nonempty": 0, "payments": 0} for _ in range(6)]
    ambiguous = []
    for block in selected:
        windows = set()
        for match in block["attempts"]:
            if match["phase"] == "offer":
                require(0 <= match["index"] < 300, "invalid primary work slot")
                windows.add(match["index"] // 50)
            else:
                require(match["phase"] in ("drain", "settle"), "unknown worker phase")
                windows.add(None)
        if None not in windows:
            primary_payments += block["payments"]
        if len(windows) > 1:
            ambiguous.append(block["hash"])
        elif None not in windows and block["payments"]:
            interval = intervals[next(iter(windows))]
            interval["selected_nonempty"] += 1
            interval["payments"] += block["payments"]
    selected_ids = {body["hash"] for body in selected}
    require(len(selected_ids) == len(selected), "duplicate active block in workload")
    stock = {r["hash"] for r in record["slots"] if r["miner"] == "sv" and "hash" in r}
    require(
        selected_ids.isdisjoint(stock), "both miners claim the same selected header"
    )
    return {
        "primary_selected_payments": primary_payments,
        "primary_intervals": intervals,
        "repeated_headers_across_intervals_or_phases": ambiguous,
        "selected_without_worker_acknowledgment": sum(
            all(a["worker_outcome"] != "solved" for a in b["attempts"])
            for b in selected
        ),
        "prepared_attempts_by_worker_outcome": dict(dispositions),
        "attempts_without_a_prepared_header": dict(without_header),
        "distinct_prepared_headers": len(attempts),
        "prepared_headers_outside_active_prefix": len(attempts.keys() - selected_ids),
        "selected_blocks": selected,
        "selected_payments": sum(b["payments"] for b in selected),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    read = gzip.open if args.receipt.suffix == ".gz" else open
    with read(args.receipt, "rt") as stream:
        print(json.dumps(audit(json.load(stream)), indent=2))
