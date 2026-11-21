"""Replay the complete confirmed workload into an empty stock SV Node on a temporary RAM disk."""

import argparse
import gzip
import hashlib
import json
import time
from collections import deque
from pathlib import Path

from mixed_corpus import REGTEST_GENESIS, path_at
from run_cohort import session_lock
from sustained_block_archive import reconstruct
from sustained_profiles import ENDORSEMENT
from sv_reference import IMAGE, Reference
from verify_receipt import require
from verify_sustained_native import FUNDING, decode_block, dsha, observed_bodies, verify

HISTORY = "3d04e4fa099e49dbe556b9d7d83d06f44264507e54f62a0d9653a3a50833a0b3"
GIB = 2**30


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def read(path):
    with (gzip.open if path.suffix == ".gz" else open)(path, "rt") as f:
        return json.load(f)


def custody(receipt, handoff):
    stock = handoff["post_window_stock_observation"]
    done = receipt["completion"]
    tips = handoff["post_window_selected_tips"]
    require(handoff["registration"] == receipt["registration"], "different handoff")
    return {
        "all_payments_settled": done["all_accounted"] and done["two_successors"],
        "all_four_handoff_tips_match": set(tips) == {"sv", "tn1", "tn2", "scary"}
        and all(
            (v["height"], v["hash"]) == (done["final_height"], done["final_hash"])
            for v in tips.values()
        ),
        "original_stock_mempool": stock.get("getmempoolinfo", {}).get("size"),
        "original_stock_validation_queue": stock.get("getblockchainactivity", {}).get(
            "transactions"
        ),
    }


def observed_wire_stream(receipt_path, corpus, blocks, archive):
    rows = observed_bodies(read(receipt_path))
    if archive is not None:
        yield from reconstruct(receipt_path, corpus, archive)
        return
    for ident, row in rows.items():
        wire = (blocks / (ident + ".block")).read_bytes()
        require(
            hashlib.sha256(wire).hexdigest() == row["wire_sha256"],
            "different retained wire",
        )
        yield row, wire


def selected_order(rows, observed, detached):
    remaining = deque(row["hash"] for row in rows)
    selected = set(remaining)
    pending = {}
    for row, wire in observed:
        ident = row["hash"]
        if ident not in selected:
            decode_block(wire, ident)
            detached[ident] = row["wire_sha256"]
            continue
        # A selected block may first have been observed on a detached branch.
        pending[ident] = row, wire
        while remaining and remaining[0] in pending:
            yield pending.pop(remaining.popleft())
    require(not remaining and not pending, "selected branch reconstruction incomplete")


def replay(history, corpus, blocks, receipt_path, handoff_path, archive=None):
    receipt = read(receipt_path)
    result = verify(receipt)
    handoff = custody(receipt, read(handoff_path))
    require(
        read(history / "manifest.json")["sha256_of_ordered_wire_digests"] == HISTORY,
        "different common history",
    )
    hits = []
    elapsed = time.monotonic()
    # The local CLI transports full blocks over stdin. External peers and paid
    # hashpower are unnecessary to reconstruct a confirmed branch.
    with Reference().running(block_bytes=4_000_000_000, tmpfs_bytes=8 * GIB) as stock:
        require(
            stock.rpc("getblockcount") == 0
            and stock.rpc("getbestblockhash") == REGTEST_GENESIS
            and stock.rpc("getconnectioncount") == 0,
            "reference did not start alone at genesis",
        )
        original = hashlib.sha256()
        previous = REGTEST_GENESIS

        def submit(wire, height):
            nonlocal previous
            identity = dsha(wire[:80])[::-1].hex()
            require(wire[4:36][::-1].hex() == previous, f"parent mismatch at {height}")
            require(
                stock.rpc("submitblock", wire.hex()) is None
                and stock.rpc("getbestblockhash") == identity,
                f"fresh stock did not select height {height}",
            )
            previous = identity

        for height in range(1, 484):
            wire = path_at(history, height).read_bytes()
            original.update(hashlib.sha256(wire).digest())
            submit(wire, height)
        require(original.hexdigest() == HISTORY, "history wire commitment differs")
        raw = (corpus / "funding.block").read_bytes()
        submit(raw, 484)
        require(previous == FUNDING, "stock selected different split funding")
        if receipt.get("arm") == "endorsed":
            submit(ENDORSEMENT, 485)
        require(previous == receipt["baseline"]["hash"], "pre-offer boundary differs")
        detached = {}
        bodies = observed_wire_stream(receipt_path, corpus, blocks, archive)
        for row, wire in selected_order(receipt["blocks"], bodies, detached):
            expected = row["wire_sha256"]
            submit(wire, row["height"])
            served = bytes.fromhex(stock.rpc("getblock", row["hash"], 0))
            require(
                served == wire,
                "fourth stock readback differs from the three measured peers",
            )
            hits.append(
                {
                    "height": row["height"],
                    "hash": previous,
                    "wire_sha256": expected,
                    "bytes": len(served),
                }
            )
        end = receipt["completion"]
        require(
            stock.rpc("getbestblockhash") == end["final_hash"]
            and stock.rpc("getblockcount") == end["final_height"],
            "final selected branch differs",
        )
        info = stock.rpc("getblockchaininfo")
        final = {
            "height": info["blocks"],
            "hash": info["bestblockhash"],
            "work": info["chainwork"],
            "mempool": stock.rpc("getmempoolinfo")["size"],
            "validation_queue": stock.rpc("getblockchainactivity")["transactions"],
            "peers": stock.rpc("getconnectioncount"),
        }
        require(
            final["mempool"] == final["validation_queue"] == final["peers"] == 0,
            "fresh stock did not finish in isolation",
        )
    return {
        "network": "regtest",
        "image": IMAGE,
        "registration": receipt["registration"],
        "arm": receipt.get(
            "arm", "inventory" if "inventory_final" in receipt else "direct"
        ),
        "measured_receipt_sha256": digest(receipt_path),
        "measured_handoff_sha256": digest(handoff_path),
        "storage": "empty 8 GiB Linux tmpfs",
        "starting_height": 0,
        "starting_hash": REGTEST_GENESIS,
        "common_history_blocks": 483,
        "common_history_wire_commitment": original.hexdigest(),
        "funding_hash": FUNDING,
        "offer_baseline": receipt["baseline"],
        "fresh_selected_full_wires": hits,
        "separately_retained_detached_merkle_wires": detached,
        "confirmed_payments": result["distinct_confirmed"],
        "terminal": final,
        "measured_custody": handoff,
        "duration_s": time.monotonic() - elapsed,
    }


def verify_replay(proof, receipt_path, handoff_path):
    receipt = read(receipt_path)
    summary = verify(receipt)
    require(
        proof["measured_receipt_sha256"] == digest(receipt_path)
        and proof["measured_handoff_sha256"] == digest(handoff_path),
        "original observations differ",
    )
    require(
        proof["image"] == IMAGE
        and proof["network"] == "regtest"
        and proof["registration"] == receipt["registration"]
        and proof["starting_hash"] == REGTEST_GENESIS
        and proof["starting_height"] == 0
        and proof["common_history_blocks"] == 483
        and proof["common_history_wire_commitment"] == HISTORY
        and proof["funding_hash"] == FUNDING
        and proof["offer_baseline"] == receipt["baseline"],
        "reference history differs",
    )
    expected = [
        {
            "height": b["height"],
            "hash": b["hash"],
            "wire_sha256": b["wire_sha256"],
            "bytes": b["wire_bytes"],
        }
        for b in receipt["blocks"]
    ]
    require(
        proof["fresh_selected_full_wires"] == expected
        and proof["confirmed_payments"] == summary["distinct_confirmed"],
        "replayed block accounting differs",
    )
    final = proof["terminal"]
    require(
        final["height"] == receipt["completion"]["final_height"]
        and final["hash"] == receipt["completion"]["final_hash"]
        and final["peers"] == final["mempool"] == final["validation_queue"] == 0,
        "fresh stock final state differs",
    )
    selected = {b["hash"] for b in receipt["blocks"]}
    require(
        proof["separately_retained_detached_merkle_wires"]
        == {
            h: b["wire_sha256"]
            for h, b in observed_bodies(receipt).items()
            if h not in selected
        },
        "detached membership differs",
    )
    original = custody(receipt, read(handoff_path))
    require(
        original == proof["measured_custody"],
        "original queues or terminal peers differ",
    )
    return {
        "fresh_selected_bodies": len(expected),
        "confirmed_payments": proof["confirmed_payments"],
        "terminal": final,
        "original_handoff": original,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "verify"))
    parser.add_argument("receipt", type=Path)
    parser.add_argument("handoff", type=Path)
    parser.add_argument("proof", type=Path)
    parser.add_argument("--history", type=Path)
    parser.add_argument("--corpus", type=Path)
    wires = parser.add_mutually_exclusive_group()
    wires.add_argument("--blocks", type=Path)
    wires.add_argument(
        "--archive", type=Path, help="compact signed-source ordinal archive"
    )
    args = parser.parse_args()
    if args.action == "run":
        if not all((args.history, args.corpus, args.blocks or args.archive)):
            parser.error(
                "run requires --history, --corpus and either --blocks or --archive"
            )
        if args.proof.exists():
            parser.error("choose a new output proof")
        with session_lock():
            proof = replay(
                args.history,
                args.corpus,
                args.blocks,
                args.receipt,
                args.handoff,
                args.archive,
            )
        tmp = args.proof.with_suffix(".next")
        tmp.write_text(json.dumps(proof, indent=2) + "\n")
        tmp.replace(args.proof)
    print(
        json.dumps(
            verify_replay(read(args.proof), args.receipt, args.handoff), indent=2
        )
    )
