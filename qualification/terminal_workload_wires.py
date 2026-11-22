"""Rebuild the three reported stock siblings and two subsequently sampled Teranode templates."""

import argparse
import gzip
import hashlib
import json
import struct
import zipfile
from pathlib import Path

from interrupted_workload import verify as verify_prefix
from prove import decode_block, dsha, parse_tx, read_varint, txid
from recover_sustained_chain import read
from sustained_block_archive import assemble, binding, put, signed_frames
from verify_receipt import check_header, require


def manifest(directory, receipt, corpus, output):
    record = read(receipt)
    verify_prefix(record)
    siblings = json.loads((directory / "sibling-dispositions.json").read_bytes())
    candidates = json.loads((directory / "candidate-wires.json").read_bytes())
    observations = {
        "stock_siblings": siblings,
        "teranode_candidates": {k: candidates[k][-1] for k in ("tn1", "tn2")},
    }
    wires = {}
    for item in siblings:
        wires["slot-" + str(item["slot"])] = directory / (item["hash"] + ".block")
    for role in observations["teranode_candidates"]:
        wires[role] = directory / (role + "-candidate.block")
    headers = {}
    hashes = {}
    for role, path in wires.items():
        wire = path.read_bytes()
        _, txs, _ = decode_block(wire, txid(wire[:80]))
        _, p = read_varint(wire, 80)
        _, _, end = parse_tx(wire, p)
        headers[role] = wire[:end]
        hashes[role] = [dsha(tx) for tx in txs[1:]]
    wanted = set().union(*hashes.values())
    indices = {}
    with (corpus / "ids.bin").open("rb") as f:
        n = 0
        while chunk := f.read(65536 * 32):
            for (h,) in struct.iter_unpack("32s", chunk):
                if h in wanted:
                    indices[h] = n
                n += 1
    require(
        wanted == indices.keys(),
        "a retained transaction is outside the registered source",
    )
    with (
        signed_frames(corpus, record["source"]) as (frames, positions),
        zipfile.ZipFile(output, "x") as z,
    ):
        put(
            z,
            "source.json",
            json.dumps(binding(receipt, frames), sort_keys=True).encode(),
        )
        put(
            z,
            "observations.json",
            json.dumps(observations, separators=(",", ":")).encode(),
        )
        for role, path in wires.items():
            members = b"".join(struct.pack("<I", indices[h]) for h in hashes[role])
            wire = path.read_bytes()
            require(
                assemble(headers[role], members, frames, positions, len(wire)) == wire,
                "different reconstructed original bytes",
            )
            put(z, role + ".prefix", headers[role])
            put(z, role + ".ordinals", members)


def verify(archive, receipt, corpus, pool_file):
    record = read(receipt)
    summary = verify_prefix(record)
    raw_pool = gzip.decompress(pool_file.read_bytes())
    require(
        hashlib.sha256(raw_pool).hexdigest()
        == summary["sorted_internal_mempool_txids_sha256"]
        and len(raw_pool) == 32 * summary["final_stock_mempool_after_interruption"],
        "different stock mempool",
    )
    pool = {item[0] for item in struct.iter_unpack("32s", raw_pool)}
    blocks = {b["height"]: b["hash"] for b in record["blocks"]}
    current = set(blocks.values())
    lost = {
        s["index"]: s
        for s in record["slots"]
        if s.get("outcome") == "solved" and s["hash"] not in current
    }
    captured_after = record["after_interruption"]["freeze_verified_wall_ns"]
    with (
        signed_frames(corpus, record["source"]) as (frames, positions),
        zipfile.ZipFile(archive) as z,
    ):
        require(
            json.loads(z.read("source.json")) == binding(receipt, frames),
            "different measured prefix or signed corpus",
        )
        obs = json.loads(z.read("observations.json"))
        names = {"source.json", "observations.json"}

        def rebuild(role, size):
            prefix, indices = role + ".prefix", role + ".ordinals"
            names.update((prefix, indices))
            return assemble(z.read(prefix), z.read(indices), frames, positions, size)

        siblings = obs["stock_siblings"]
        require(
            len(siblings) == len(lost) and {x["slot"] for x in siblings} == lost.keys(),
            "unaccounted sibling solution",
        )
        for x in siblings:
            slot = lost[x["slot"]]
            role = "slot-" + str(x["slot"])
            raw = rebuild(role, x["wire_bytes"])
            digest = hashlib.sha256(raw).hexdigest()
            require(
                x["stock_read_wall_ns"] > captured_after
                and x["miner"] == slot["miner"] == "sv"
                and x["hash"] == slot["hash"]
                and x["header"] == raw[:80].hex()
                and digest == x["wire_sha256"],
                "different stock sibling bytes or timing",
            )
            check_header(raw[:80], x["hash"], slot["parent"])
            _, txs, _ = decode_block(raw, x["hash"])
            require(
                x["ordinary_payments"] == len(txs) - 1
                and x["confirmations"] == -1
                and blocks[x["height"]] == x["active_at_height"] != x["hash"],
                "sibling disposition differs",
            )
            require(
                set(x["teranode"]) == {"tn1", "tn2"}
                and all(
                    v["sha256"] == digest
                    and v["bytes"] == len(raw)
                    and v["http_status"] == 200
                    for v in x["teranode"].values()
                ),
                "stock peer returned different sibling bytes",
            )
        c = obs["teranode_candidates"]
        require(set(c) == {"tn1", "tn2"}, "missing independent Teranode report")
        ordered = {}
        for role, x in c.items():
            raw = rebuild(role, x["wire_bytes"])
            rpc = x["rpc"]
            require(
                x["wall_ns"] > captured_after
                and x["http_status"] == 200
                and hashlib.sha256(raw).hexdigest() == x["wire_sha256"]
                and raw[4:36][::-1].hex()
                == rpc["prevhash"]
                == summary["final_audited_hash"]
                and rpc["height"] == summary["final_audited_height"] + 1,
                "candidate parent, address or bytes differ",
            )
            _, txs, _ = decode_block(raw, txid(raw[:80]))
            ids = [dsha(tx) for tx in txs[1:]]
            require(
                bytes.fromhex(rpc["coinbase"]) == txs[0]
                and len(txs) == x["total"]
                and len(ids) == len(set(ids)) == rpc["num_tx"] == x["ordinary"],
                "candidate count or RPC coinbase differs",
            )
            require(
                len(set(ids) & pool) == x["in_stock_mempool"]
                and hashlib.sha256(b"".join(ids)).hexdigest()
                == x["ordered_internal_txids_sha256"],
                "candidate membership or order differs from readback",
            )
            ordered[role] = ids
        require(
            len(z.infolist()) == len(names) and set(z.namelist()) == names,
            "unexpected archive members",
        )
    a, b = ordered["tn1"], ordered["tn2"]
    return {
        "stock_sibling_heights": [x["height"] for x in siblings],
        "stock_sibling_bytes": sum(x["wire_bytes"] for x in siblings),
        "exact_sibling_reads": 3 * len(siblings),
        "candidate_bytes": {k: v["wire_bytes"] for k, v in c.items()},
        "ordinary_per_candidate": {k: len(v) for k, v in ordered.items()},
        "candidate_membership_intersection": len(set(a) & set(b)),
        "same_order": a == b,
        "payments_at_the_same_position": sum(x == y for x, y in zip(a, b)),
        "in_original_stock_mempool": {
            k: len(set(v) & pool) for k, v in ordered.items()
        },
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("pack", "verify"))
    p.add_argument("receipt", type=Path)
    p.add_argument("corpus", type=Path)
    p.add_argument("pool_ids", type=Path)
    p.add_argument("archive", type=Path)
    p.add_argument("--original-bodies", type=Path)
    a = p.parse_args()
    if a.command == "pack":
        if not a.original_bodies:
            p.error("provide --original-bodies for packing")
        manifest(a.original_bodies, a.receipt, a.corpus, a.archive)
    print(json.dumps(verify(a.archive, a.receipt, a.corpus, a.pool_ids), indent=2))
