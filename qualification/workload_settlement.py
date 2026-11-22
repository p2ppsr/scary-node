"""Trace the final offer, wallet counters and selected settlement blocks from a closed receipt."""

import argparse
import json
from pathlib import Path

from recover_sustained_chain import read
from verify_sustained_native import verify


def trace(record):
    checked = verify(record)
    offers = [r for r in record["producer"] if r["phase"] == "offer"]
    drain = [r for r in record["producer"] if r["phase"] == "rebroadcast"]
    served = next(
        (
            r
            for r in record.get("inventory_progress", [])
            if r["unique_served"] == checked["registered_payments"]
        ),
        None,
    )
    last = record["completion"]["last_payment_height"]
    by_hash = {r["hash"]: r for r in record["slots"] if r.get("outcome") == "solved"}
    tail = []
    for b in record["blocks"]:
        if b["height"] < last:
            continue
        slot = by_hash.get(b["hash"])
        tail.append(
            {
                "height": b["height"],
                "hash": b["hash"],
                "payments": b["scheduled_payments"],
                "wire_bytes": b["wire_bytes"],
                "matching_wire_reads": b["independent_wire_sha256"],
                "first_sampled_tip_ns": b["first_observed_ns"],
                "completed_body_audit_ns": b["readback_ns"],
                "scheduled_solution_slot": slot["index"] if slot else None,
            }
        )
    return {
        "arm": record.get("arm"),
        "start_wall_ns": record["start_wall_ns"],
        "last_offer": offers[-1] if offers else None,
        "first_sample_with_all_wallet_transfers_served": served,
        "readvertisement_batches": len(drain),
        "readvertisement_entries": sum(r["count"] for r in drain),
        "first_readvertisement": drain[0] if drain else None,
        "last_readvertisement": drain[-1] if drain else None,
        "final_wallet_counters": record.get("inventory_final"),
        "last_payment_and_successors": tail,
        "primary_close_ns": record["duration_ns"],
        "full_denominator_seconds": checked["seconds_through_end"],
        "confirmed_payments_per_second": checked["confirmed_spends_per_second"],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    print(json.dumps(trace(read(p.parse_args().receipt)), indent=2))
