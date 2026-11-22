"""Replay the native decisions and full-node observations on each side of a mainnet restart."""

import argparse
import gzip
import hashlib
import json
from datetime import datetime
from pathlib import Path

from shadow import merkle
from shadow_checkpoint import SCALE, replay, target
from shadow_first_candidate import fetch_block
from shadow_inclusion import require, transaction_ids
from shadow_native import proofs, read_varint


def read(path):
    with gzip.open(path, "rt") as source:
        return json.load(source)


def verify(checkpoint, receipt, block_dir=None, assets=()):
    summary = replay(checkpoint, assess=True)
    require(
        summary["observations_sha256"] == receipt["checkpoint_sha256"],
        "checkpoint digest differs",
    )
    data = read(checkpoint)
    records = data["records"]
    starts = [
        i
        for i, row in enumerate(records)
        if row.get("event") == "start" and row["at"] == receipt["restart_at"]
    ]
    require(len(starts) == 1, "restart absent or ambiguous")
    i = starts[0]
    start = records[i]
    parent = next(row for row in reversed(records[:i]) if row.get("event") == "tip")
    last_report = next(
        row for row in reversed(records[:i]) if row.get("event") == "reference_tip"
    )
    child_index = next(
        j for j in range(i + 1, len(records)) if records[j].get("event") == "tip"
    )
    child = records[child_index]
    require(
        start["restored_tip"] == parent["hash"] == child["previous"]
        and child["height"] == parent["height"] + 1,
        "first resumed block did not directly extend the last observed parent",
    )
    reports = [
        row
        for row in records[i + 1 : child_index]
        if row.get("event") == "reference_tip"
    ]
    require(
        reports
        and all(row["hash"] in (parent["hash"], child["hash"]) for row in reports),
        "unexpected peer tip at the boundary",
    )
    child_ref = data["reference"]["by_hash"][child["hash"]]
    parent_work = int(child_ref["chainwork"], 16) - SCALE // (target(child["bits"]) + 1)
    interim = receipt["independent_tip_reads"]
    require(
        interim
        and all(
            row["height"] == parent["height"]
            and row["bestblockhash"] == parent["hash"]
            and int(row["chainwork"], 16) == parent_work
            and start["at"]
            <= datetime.fromisoformat(row["read_at_utc"]).timestamp()
            < child["observed_at"]
            for row in interim
        ),
        "independent interim tip or chainwork differs",
    )
    decisions = []
    checked_bytes = 0
    for tip, observation in zip((parent, child), receipt["blocks"], strict=True):
        header = bytes.fromhex(tip["header_hex"])
        ident = tip["hash"]
        reference = observation["reference_transaction_list"]
        ids = transaction_ids(reference["transactions"])
        require(
            ident == observation["hash"] == reference["hash"]
            and tip["height"] == reference["height"]
            and reference["confirmed_at_read"] > 0
            and ids
            and merkle([bytes.fromhex(t)[::-1] for t in ids]) == header[36:68],
            "independent confirmed transaction list differs from the native header",
        )
        if block_dir or assets:
            raw = (
                (block_dir / ident).read_bytes()
                if block_dir
                else fetch_block(ident, assets, observation["bytes"])
            )
            require(
                raw is not None
                and len(raw) == observation["bytes"]
                and hashlib.sha256(raw).hexdigest() == observation["sha256"]
                and raw[:80] == header
                and read_varint(raw, 80)[0] == len(ids),
                "confirmed wire bytes differ from the original SV P2P and Teranode readbacks",
            )
            proofs(raw, tip["height"], ident, set())
            checked_bytes += len(raw)
        notices = [row for row in reports if row["hash"] == ident]
        require(
            notices and all(row["height"] == tip["height"] for row in notices),
            "peer heights differ",
        )
        decisions.append(
            {
                "height": tip["height"],
                "transactions": len(ids),
                "coverage": tip["coverage"],
                "ready": tip["ready"],
                "unsigned_candidate": bool(tip["unsigned_candidate"]),
                "peer_reports": len(notices),
                "distinct_peer_ids": len({row["peer"] for row in notices}),
                "first_report_unix_s": min(row["observed_at"] for row in notices),
            }
        )
    return {
        "decisions": decisions,
        "restore_matches_parent": True,
        "seconds_between_local_peer_reports": reports[0]["observed_at"]
        - last_report["observed_at"],
        "first_successor_report_to_decision_seconds": child["observed_at"]
        - decisions[1]["first_report_unix_s"],
        "independently_replayed_body_bytes": checked_bytes,
        "qualifying_window_blocks": summary["qualification"][
            "qualifying_window_blocks"
        ],
        "qualified": summary["qualification"]["qualified"],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("checkpoint", type=Path)
    p.add_argument("receipt", type=Path)
    sources = p.add_mutually_exclusive_group()
    sources.add_argument(
        "--block-dir", type=Path, help="complete confirmed wires named by block hash"
    )
    sources.add_argument(
        "--asset",
        action="append",
        default=[],
        help="Teranode Asset base including /api/v1",
    )
    a = p.parse_args()
    print(
        json.dumps(
            verify(a.checkpoint, read(a.receipt), a.block_dir, a.asset), indent=2
        )
    )
