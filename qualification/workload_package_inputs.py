"""Recover external input ages from a verified compact archive of the signed payment rails."""

import argparse
import hashlib
import json
import struct
import zipfile
from array import array
from collections import Counter
from pathlib import Path

from recover_sustained_chain import read
from sustained_outcomes import digest
from verify_sustained_native import FUNDING, ORDERED_SOURCE, PAYMENTS, require
from workload_solved_ledger import audit


def participant_counts(blocks, rails, funding_height, limit):
    """Each signed spend consumes the previous generation on its own funding rail."""
    next_generation = array("I", [0]) * rails
    previous_height = array("I", [funding_height]) * rails
    root_distance = array("I", [0]) * rails
    selected = []
    for row, ordinals in blocks:
        counts = Counter()
        external = Counter()
        height = row["height"]
        for index in ordinals:
            require(0 <= index < limit, "unregistered payment ordinal")
            generation, rail = divmod(index, rails)
            require(
                generation == next_generation[rail],
                "missing parent or repeated rail spend",
            )
            age = height - previous_height[rail]
            require(age >= 0, "payment precedes its parent")
            if age:
                root_distance[rail] = age
            require(root_distance[rail] > 0, "missing external input")
            counts[age] += 1
            external[root_distance[rail]] += 1
            previous_height[rail] = height
            next_generation[rail] += 1
        require(sum(counts.values()) == row["payments"], "block payment count differs")
        selected.append(
            {
                **{k: row[k] for k in ("height", "hash", "owner", "payments")},
                "direct_parent_age": dict(sorted(counts.items())),
                "external_ancestor_age": dict(sorted(external.items())),
            }
        )
    return selected


def analyze(receipt, archive, ledger, archive_sha256):
    record = read(receipt)
    origin = audit(record, read(ledger))["selected"]
    source = record.get("setup", {}).get("funding", record["baseline"])
    require(
        source["hash"] == FUNDING and source["height"] == 484, "different split funding"
    )
    require(digest(archive) == archive_sha256, "different compact archive")
    receipt_hash = hashlib.sha256(receipt.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read("source.json"))
        require(
            manifest["study_receipt_sha256"] == receipt_hash
            and manifest["ordered_txids_sha256"] == ORDERED_SOURCE,
            "archive source or study differs",
        )

        def blocks():
            for measured, role in zip(record["blocks"], origin, strict=True):
                require(measured["hash"] == role["hash"], "different origin record")
                path = role["hash"] + ".ordinals"
                require(
                    z.getinfo(path).file_size == role["payments"] * 4,
                    "different source index length",
                )
                require(
                    measured["ordinary_transactions"] == role["payments"],
                    "selected non-corpus payment",
                )
                yield role, (i for (i,) in struct.iter_unpack("<I", z.read(path)))

        rows = participant_counts(blocks(), 65_536, source["height"], PAYMENTS)
    totals = {}
    for row in rows:
        role = totals.setdefault(
            row["owner"],
            {
                "selected_blocks": 0,
                "payments": 0,
                "same_block_parent": 0,
                "immediate_external_input": 0,
                "endorsed_external_input": 0,
            },
        )
        role["selected_blocks"] += 1
        role["payments"] += row["payments"]
        role["same_block_parent"] += row["direct_parent_age"].get(0, 0)
        immediate = row["external_ancestor_age"].get(1, 0)
        role["immediate_external_input"] += immediate
        role["endorsed_external_input"] += row["payments"] - immediate
    return {
        "registration": record["registration"],
        "receipt_sha256": receipt_hash,
        "verified_compact_archive_sha256": archive_sha256,
        "by_owner": totals,
        "blocks": rows,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("receipt", "archive", "ledger"):
        p.add_argument(name, type=Path)
    p.add_argument(
        "--archive-sha256",
        required=True,
        help="SHA-256 from the full signed-wire replay",
    )
    args = p.parse_args()
    print(
        json.dumps(
            analyze(args.receipt, args.archive, args.ledger, args.archive_sha256),
            indent=2,
        )
    )
