"""Read an entrant checkpoint and reconcile its own solved hashes with the stock branch."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from interrupted_workload import SCHEMA
from interrupted_workload import verify as verify_interruption
from recover_sustained_chain import read
from verify_sustained_native import check_header, dsha, require, verify

FIELDS = (
    "hash",
    "parent",
    "height",
    "job",
    "queued_at_ms",
    "published_at_ms",
    "first_inclusion_at_ms",
    "first_settlement_at_ms",
    "chain_state",
)


def extract(path, binding=None):
    import msgpack

    if binding is None:
        candidates = list(path.parent.glob("mining-domain-*"))
        require(
            len(candidates) == 1, "specify the corresponding mining-domain binding file"
        )
        binding = candidates[0]
    identity = binding.name.removeprefix("mining-domain-")
    require(
        binding.name.startswith("mining-domain-")
        and len(identity) == 64
        and bytes.fromhex(identity).hex() == identity
        and binding.stat().st_size == 32,
        "invalid mining-domain binding",
    )
    size = path.stat().st_size
    selected = {
        "height",
        "solved_blocks",
        "lifecycle",
        "headers",
        "solution_body",
    }
    with path.open("rb") as source:
        require(
            source.read(8) == b"SCARYWS1" and size > 40, "invalid checkpoint framing"
        )
        digest = hashlib.sha256()
        remaining = size - 40
        while remaining:
            chunk = source.read(min(remaining, 1 << 20))
            require(chunk, "truncated checkpoint")
            digest.update(chunk)
            remaining -= len(chunk)
        require(source.read() == digest.digest(), "checkpoint payload checksum differs")
        source.seek(8)
        unpacker = msgpack.Unpacker(
            source, raw=False, strict_map_key=False, max_buffer_size=1 << 30
        )
        fields = {}
        for _ in range(unpacker.read_map_header()):
            name = unpacker.unpack()
            if name in selected:
                require(name not in fields, "duplicate checkpoint field")
                fields[name] = unpacker.unpack()
            else:
                unpacker.skip()
        require(
            unpacker.tell() == size - 40 and fields.keys() == selected,
            "unexpected checkpoint structure",
        )
    asset = fields["solution_body"]
    result = None
    if asset:
        raw = bytes(asset["raw"])
        block_hash = bytes(asset["hash"])[::-1].hex()
        check_header(raw[:80], block_hash)
        result = {
            "height": asset["height"],
            "hash": block_hash,
            "header": raw[:80].hex(),
            "wire_bytes": len(raw),
            "wire_sha256": hashlib.sha256(raw).hexdigest(),
        }
    return {
        "checkpoint_bytes": size,
        "checkpoint_payload_sha256": digest.hexdigest(),
        "mining_identity": identity,
        "binding_file_sha256": hashlib.sha256(binding.read_bytes()).hexdigest(),
        "tip_height": fields["height"],
        "tip_hash": dsha(bytes(fields["headers"][-1]))[::-1].hex(),
        "solved_blocks": fields["solved_blocks"],
        "records": fields["lifecycle"]["records"],
        "retained_solution_asset": result,
    }


def public_ledger(captured):
    # The member labels are stable for this checkpoint and reveal no addresses.
    names = sorted(
        {
            name
            for row in captured["records"]
            for name in [*row["peer_tip_reporters"], *row["deliveries"]]
        }
    )
    peers = {
        name: ("sv" if name.startswith("sv:") else "native") + "-peer-" + str(i + 1)
        for i, name in enumerate(names)
    }
    records = []
    for row in captured["records"]:
        fields = {key: row[key] for key in FIELDS}
        fields["independent_tip_reporters"] = len(set(row["peer_tip_reporters"]))
        fields["tip_reporters"] = sorted(
            peers[name] for name in row["peer_tip_reporters"]
        )
        fields["deliveries"] = {
            peers[name]: dict(sorted(events.items()))
            for name, events in sorted(row["deliveries"].items())
        }
        fields["transitions"] = [
            {key: item[key] for key in ("at_ms", "state", "height")}
            for item in row["transitions"]
        ]
        records.append(fields)
    return {
        **{
            key: captured[key]
            for key in (
                "binding_file_sha256",
                "checkpoint_bytes",
                "checkpoint_payload_sha256",
                "mining_identity",
                "tip_height",
                "tip_hash",
                "solved_blocks",
                "retained_solution_asset",
            )
        },
        "records": records,
    }


def audit(record, ledger):
    if record.get("schema") == SCHEMA:
        verify_interruption(record)
    else:
        verify(record)
    chain = {row["hash"]: row for row in record["blocks"]}
    tip = record["blocks"][-1]
    require(
        (tip["height"], tip["hash"]) == (ledger["tip_height"], ledger["tip_hash"]),
        "checkpoint tip differs",
    )
    local = {row["hash"]: row for row in ledger["records"]}
    require(
        len(local) == len(ledger["records"]) == ledger["solved_blocks"] < 256,
        "solution journal is incomplete or has duplicate hashes",
    )
    slots = record["slots"]
    acknowledged = {}
    for row in slots:
        if row.get("hash"):
            require(row["hash"] not in acknowledged, "duplicate acknowledged hash")
            acknowledged[row["hash"]] = row
            if row["miner"] == "scary":
                require(
                    row["hash"] in local,
                    "worker-returned Scary hash missing from journal",
                )
                require(
                    local[row["hash"]]["parent"] == row["parent"],
                    "solution parent differs",
                )
            else:
                require(row["hash"] not in local, "stock hash in entrant journal")
    errors = [
        row for row in slots if row["miner"] == "scary" and row["outcome"] == "error"
    ]
    inferred = {}
    modern = record.get("worker_header_format") == 1
    if modern:
        from workload_worker_headers import audit as verify_attempts

        verify_attempts(record)
    for digest, solved in local.items():
        if digest in acknowledged:
            continue
        if modern:
            candidates = [
                row
                for row in slots
                if row.get("worker_candidate", {}).get("hash") == digest
            ]
            for candidate in candidates:
                require(
                    candidate["worker_candidate"]["notify"][0] == solved["job"],
                    "worker and journal job IDs differ",
                )
        else:
            candidates = [
                e
                for e in errors
                if e.get("ready") and e.get("parent") == solved["parent"]
            ]
        if len(candidates) == 1:
            inferred[digest] = candidates[0]
    multiplicity = Counter((row["phase"], row["index"]) for row in inferred.values())
    primary = Counter()
    totals = {}
    rows = []
    for digest, block in chain.items():
        wire = bytes.fromhex(block["header_hex"])
        check_header(wire, digest)
        require(
            block["independent_wire_sha256"]
            == dict.fromkeys(("sv", "tn1", "tn2"), block["wire_sha256"]),
            "missing independent full body commitment",
        )
        returned = acknowledged.get(digest)
        slot = returned or inferred.get(digest)
        if slot and not returned and multiplicity[(slot["phase"], slot["index"])] != 1:
            slot = None
        if digest in local:
            solved = local[digest]
            require(
                solved["parent"] == wire[4:36][::-1].hex()
                and solved["height"] == block["height"],
                "independently read selected header differs from local solution",
            )
            owner = "scary"
        else:
            owner = returned["miner"] if returned else "unattributed"
        row = {
            "height": block["height"],
            "hash": digest,
            "owner": owner,
            "in_solved_journal": digest in local,
            "worker_returned_hash": bool(returned),
            "slot_attribution": (
                "returned hash"
                if returned
                else "prepared worker header"
                if slot and modern
                else "sole ready same-parent worker error"
                if slot
                else "unassigned"
            ),
            "worker_phase": slot["phase"] if slot else None,
            "worker_index": slot["index"] if slot else None,
            "worker_outcome": slot["outcome"] if slot else None,
            "payments": block["scheduled_payments"],
        }
        rows.append(row)
        group = totals.setdefault(owner, Counter())
        group["blocks"] += 1
        group["payments"] += row["payments"]
        if owner == "scary" and slot and slot["phase"] == "offer":
            primary["blocks"] += 1
            primary["payments"] += row["payments"]
    asset = ledger["retained_solution_asset"]
    if asset:
        require(
            asset["hash"] in local, "last retained body missing from solution journal"
        )
        if asset["hash"] in chain:
            selected = chain[asset["hash"]]
            require(
                (
                    selected["wire_bytes"],
                    selected["wire_sha256"],
                    selected["header_hex"],
                )
                == (asset["wire_bytes"], asset["wire_sha256"], asset["header"]),
                "last selected solved asset differs from stock bytes",
            )
    error_rows = []
    for error in errors:
        children = [
            s["hash"] for s in local.values() if s["parent"] == error.get("parent")
        ]
        error_rows.append(
            {
                "phase": error["phase"],
                "index": error["index"],
                "parent": error.get("parent"),
                "ready": error.get("ready"),
                "reason": error.get("reason"),
                "same_parent_journal_hashes": sorted(children),
                "selected_journal_children": sorted(h for h in children if h in chain),
            }
        )
    windows = [
        {"slots": [start, start + 49], "selected_nonempty": 0, "payments": 0}
        for start in range(0, 300, 50)
    ]
    for row in rows:
        if row["owner"] == "scary" and row["worker_phase"] == "offer":
            group = windows[row["worker_index"] // 50]
            group["selected_nonempty"] += row["payments"] > 0
            group["payments"] += row["payments"]
    return {
        "primary_by_five_minute_window": windows,
        "selected_totals": totals,
        "scary_primary": primary,
        "selected": rows,
        "worker_errors": error_rows,
        "checkpoint_solutions": len(local),
        "selected_checkpoint_solutions": len(local.keys() & chain.keys()),
    }


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="action", required=True)
    raw = commands.add_parser("extract")
    raw.add_argument("checkpoint", type=Path)
    raw.add_argument("--binding", type=Path)
    verify_cli = commands.add_parser("verify")
    verify_cli.add_argument("receipt", type=Path)
    verify_cli.add_argument("ledger", type=Path)
    args = cli.parse_args()
    if args.action == "extract":
        result = public_ledger(extract(args.checkpoint, args.binding))
    else:
        result = audit(read(args.receipt), read(args.ledger))
    print(json.dumps(result, indent=2))
