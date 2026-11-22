"""Compare fixed native mining opportunities against each receipt's final audited chain."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from interrupted_workload import SCHEMA
from interrupted_workload import verify as verify_interruption
from recover_sustained_chain import read
from verify_sustained_native import SLOTS, require, verify


def summarize(path):
    record = read(path)
    interrupted = record.get("schema") == SCHEMA
    checked = verify_interruption(record) if interrupted else verify(record)
    selected = {row["hash"]: row for row in record["blocks"]}
    by_index = {row["index"]: row for row in record["slots"] if row["phase"] == "offer"}
    windows = []
    for start in range(0, SLOTS, 50):
        rows = [by_index[i] for i in range(start, start + 50) if i in by_index]
        scary = [row for row in rows if row["miner"] == "scary"]
        mined = [
            selected[row["hash"]]["scheduled_payments"]
            for row in scary
            if row["outcome"] == "solved" and row["hash"] in selected
        ]
        unready = [row for row in scary if row["outcome"] == "unready"]
        windows.append(
            {
                "slots": [start, start + 49],
                "recorded_slots": len(rows),
                "unobserved_slots": 50 - len(rows),
                "recorded_scary_outcomes": dict(
                    Counter(row["outcome"] for row in scary)
                ),
                "scary_unready_reasons": dict(
                    Counter(
                        row.get("job_wait_reason") or "reason unavailable"
                        for row in unready
                    )
                ),
                "scary_unready_with_ready_receiver": sum(
                    row.get("receiver_ready") is True for row in unready
                ),
                "scary_unready_with_unready_receiver": sum(
                    row.get("receiver_ready") is False for row in unready
                ),
                "scary_unready_without_receiver_status": sum(
                    row.get("receiver_ready") is None for row in unready
                ),
                "worker_return_matched_nonempty_scary_blocks": sum(
                    n > 0 for n in mined
                ),
                "worker_return_matched_payments_in_scary_blocks": sum(mined),
            }
        )
    registered = record.get("arm")
    primary = sum(w["recorded_slots"] for w in windows)
    if interrupted:
        require(
            primary == checked["recorded_opportunities"],
            "observed opportunity count differs",
        )
        amount = checked["confirmed_payments"]
        throughput = depth = elapsed = None
        regular = False if checked["per_window_criterion_already_missed"] else None
    else:
        amount = checked["distinct_confirmed"]
        throughput = checked["confirmed_spends_per_second"]
        elapsed = checked["seconds_through_end"]
        depth = checked["last_payment_settled_depth_two"]
        regular = checked.get("regular_entrant_origin")
        if registered and "entrant_primary_by_five_minute_slot_interval" in checked:
            require(
                [
                    {
                        "selected_nonempty": w[
                            "worker_return_matched_nonempty_scary_blocks"
                        ],
                        "payments": w["worker_return_matched_payments_in_scary_blocks"],
                    }
                    for w in windows
                ]
                == checked["entrant_primary_by_five_minute_slot_interval"],
                "window accounting differs from the registered verifier",
            )
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "arm": registered or ("inventory" if "inventory_final" in record else "direct"),
        "registration": record["registration"],
        "receipt_sha256": digest,
        "release_sha256": record["release_sha256"],
        "starting_height": record["baseline"]["height"],
        "scheduled_primary_slots": SLOTS,
        "recorded_primary_slots": primary,
        "interrupted": interrupted,
        "distinct_audited_payments": amount,
        "seconds_through_final_observation": elapsed,
        "confirmed_payments_per_second": throughput,
        "two_successors_after_final_payment": depth,
        "registered_regular_origin": regular,
        "registered_joint_target": regular
        if interrupted
        else checked.get("witnessed_miner_target_met"),
        "windows": windows,
    }


if __name__ == "__main__":
    arg = argparse.ArgumentParser(description=__doc__)
    arg.add_argument("receipts", nargs="+", type=Path)
    args = arg.parse_args()
    print(json.dumps([summarize(path) for path in args.receipts], indent=2))
