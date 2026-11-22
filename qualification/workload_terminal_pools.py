"""Check both original post-window pool observations against the sealed regtest tip."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path

from recover_sustained_chain import read
from verify_sustained_native import decode_block, dsha, require


def build(receipt, path):
    rows = read(path)
    proof = {
        "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        "original_handoff_wall_ns": rows["original_handoff_wall_ns"],
        "original_refrozen_wall_ns": rows["refrozen_wall_ns"],
        "samples": [],
    }
    for sample in rows["samples"]:
        member = {
            "started_wall_ns": sample["wall_ns"],
            "finished_wall_ns": sample["finished_wall_ns"],
            "sv": {
                k: sample["stock"][k]
                for k in (
                    "height",
                    "selected",
                    "membership_count",
                    "membership_internal_sha256",
                )
            },
            "scary": {
                k: sample["scary"][k]
                for k in (
                    "height",
                    "tip_hash",
                    "pending_transactions",
                    "solved_blocks",
                    "witnessed_outputs",
                )
            },
        }
        pool = gzip.decompress(
            (
                path.parent
                / (
                    "stock-pending-"
                    + member["sv"]["membership_internal_sha256"]
                    + ".sorted-internal.bin.gz"
                )
            ).read_bytes()
        )
        require(
            len(pool) == 32 * member["sv"]["membership_count"]
            and hashlib.sha256(pool).hexdigest()
            == member["sv"]["membership_internal_sha256"],
            "original pending membership differs",
        )
        for role in ("tn1", "tn2"):
            native = sample[role]
            result = native["calls"][-1]
            wire = (path.parent / result["wire_file"]).read_bytes()
            require(
                result["http"] == 200 and len(wire) == result["bytes"],
                "incomplete candidate read",
            )
            member[role] = {
                "selected": native["selected"],
                "height": native["height"],
                "ordinary": result["ordinary"],
                "ordered_internal_ids_sha256": result["ordered_internal_ids_sha256"],
                "candidate_header": wire[:80].hex(),
                "candidate_wire_sha256": hashlib.sha256(wire).hexdigest(),
                "started_wall_ns": result["started_wall_ns"],
                "finished_wall_ns": result["finished_wall_ns"],
            }
            # A settled empty queue fits in the receipt without an external candidate asset.
            if result["ordinary"] == 0:
                member[role]["candidate_wire_hex"] = wire.hex()
        proof["samples"].append(member)
    verify(receipt, proof)
    return proof


def verify(receipt, proof):
    record = read(receipt)
    tip = record["blocks"][-1]
    require(
        hashlib.sha256(receipt.read_bytes()).hexdigest() == proof["receipt_sha256"]
        and len(proof["samples"]) == 2,
        "terminal observation and study differ",
    )
    end = proof["original_handoff_wall_ns"]
    require(
        end
        >= record["start_wall_ns"]
        + max(record["duration_ns"], *(b["readback_ns"] for b in record["blocks"])),
        "terminal check precedes closure",
    )
    for sample in proof["samples"]:
        require(
            end
            <= sample["started_wall_ns"]
            < sample["finished_wall_ns"]
            <= proof["original_refrozen_wall_ns"],
            "post-window times overlap",
        )
        end = sample["finished_wall_ns"]
        sv = sample["sv"]
        entrant = sample["scary"]
        require(
            int(sv["height"]) == entrant["height"] == tip["height"]
            and sv["selected"] == entrant["tip_hash"] == tip["hash"],
            "stock or entrant tip differs",
        )
        for role in ("tn1", "tn2"):
            n = sample[role]
            require(
                int(n["height"]) == tip["height"] and n["selected"] == tip["hash"],
                "native tip differs",
            )
            require(
                sample["started_wall_ns"]
                <= n["started_wall_ns"]
                <= n["finished_wall_ns"]
                <= end,
                "candidate sampled outside window",
            )
            if "candidate_wire_hex" in n:
                raw = bytes.fromhex(n["candidate_wire_hex"])
                _, txs, _ = decode_block(raw, dsha(raw[:80])[::-1].hex())
                require(
                    raw[:80].hex() == n["candidate_header"]
                    and raw[4:36][::-1].hex() == tip["hash"]
                    and hashlib.sha256(raw).hexdigest() == n["candidate_wire_sha256"]
                    and len(txs) - 1 == n["ordinary"]
                    and hashlib.sha256(b"".join(dsha(tx) for tx in txs[1:])).hexdigest()
                    == n["ordered_internal_ids_sha256"],
                    "unpublished candidate bytes differ",
                )
    return [
        {r: s[r]["ordinary"] for r in ("tn1", "tn2")}
        | {
            "sv": s["sv"]["membership_count"],
            "scary": s["scary"]["pending_transactions"],
        }
        for s in proof["samples"]
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "verify"))
    parser.add_argument("receipt", type=Path)
    parser.add_argument("proof", type=Path)
    args = parser.parse_args()
    result = (
        build(args.receipt, args.proof)
        if args.action == "build"
        else verify(args.receipt, read(args.proof))
    )
    print(json.dumps(result, indent=2))
