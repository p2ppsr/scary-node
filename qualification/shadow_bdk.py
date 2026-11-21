"""Replay a captured mainnet candidate's ordinary scripts in upstream GoBDK over local stdin."""

import argparse
import contextlib
import hashlib
import json
import struct
import subprocess
from pathlib import Path

from shadow_first_candidate import assess, load
from shadow_native import inputs, require, txid, verify_funding
from shadow_native_warmup import verify as verify_warmup
from verify_entrant_chronicle import compact, extend

BDK = "8a4e1d3ddb16cbb2b3a98f82ea1ee6ff19a297e9"
SV = "6504a3aff65ba97c0f6c80962b033e35ecbfed4b"
DISTRIBUTED_BDK = "9d34cf20a727185286a686a7800f964c34a55525"


def coin(height, value, script):
    return (height, struct.pack("<Q", value) + compact(len(script)) + script)


def warmup(record):
    measurements = verify_warmup(record)
    coins, payment = {}, None
    for group in record["proofs"]:
        h = txid(bytes.fromhex(group["header"]))
        for tid, outputs in verify_funding(group, h).items():
            if tid == record["spend"]:
                payment = group["transactions"][tid]["tx"]
            else:
                for i, (value, script) in enumerate(outputs):
                    coins[tid, i] = coin(group["height"], value, script)
    require(payment, "missing confirmed spend bytes")
    return measurements["spend_height"] - 1, [payment], coins


def candidate(record):
    decision = assess(record)
    require(
        decision["complete"], "candidate and anchored funding evidence are incomplete"
    )
    header = bytes.fromhex(record["decision"]["unsigned_candidate"]["header_hex"])
    require(
        int.from_bytes(header[:4], "little", signed=True) >= 4,
        "obsolete mainnet candidate version",
    )
    coins = {}
    for outpoint, p in record["witnessed"].items():
        tid, index = outpoint.split(":")
        coins[tid, int(index)] = coin(
            p["height"], p["value"], bytes.fromhex(p["script"])
        )
    return decision["parent_height"], record["raw_transactions"], coins


def ask(process, raw, coins, parent):
    ef, heights = extend(raw, coins)
    require(
        943816 <= parent < 2**31 - 1
        and heights
        and all(943816 <= h <= parent + 1 for h in heights),
        "ordinary replay requires current-era mainnet input heights",
    )
    row = {"extended": ef.hex(), "heights": heights, "parent": parent}
    process.stdin.write(json.dumps(row, separators=(",", ":")) + "\n")
    process.stdin.flush()
    result = json.loads(process.stdout.readline())
    require(
        all(type(result.get(k)) is bool for k in ("policy", "consensus")),
        "incomplete GoBDK response",
    )
    return result


def replay(record, executable, native="source", command=None):
    confirmed = record.get("kind") == "confirmed-warmup-spend"
    parent, raws, coins = warmup(record) if confirmed else candidate(record)
    policy_count = consensus_count = skipped = 0
    disagreements = []
    with contextlib.ExitStack() as stack:
        proc = stack.enter_context(
            subprocess.Popen(
                command or [executable],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
        )
        stack.callback(lambda: proc.kill() if proc.poll() is None else None)
        build = json.loads(proc.stdout.readline())
        for key, pin in (
            ("source", BDK if native == "source" else DISTRIBUTED_BDK),
            ("sv_source", SV),
        ):
            reported = build[key]
            require(
                len(reported) >= 7 and pin.startswith(reported),
                "unrecognized GoBDK source",
            )
        require(
            (build["network"], build["bdk_version"], build["sv_version"])
            == ("main", "1.4.0", "1.2.3"),
            "unexpected native interpreter or network",
        )
        for wire in raws:
            raw = bytes.fromhex(wire)
            tid = txid(raw)
            points, outputs = inputs(raw)
            if outputs[0][1].startswith(b"\x00\x6a\x04cftx"):
                skipped += 1
            else:
                result = ask(proc, raw, coins, parent)
                policy_count += result["policy"]
                consensus_count += result["consensus"]
                if not all(result[k] for k in ("policy", "consensus")):
                    disagreements.append({"txid": tid, **result})
            if confirmed:
                # This particular collected spend uses SIGHASH_ALL|FORKID.
                # Its signature commits to the original previous output amount.
                altered = dict(coins)
                height, source = altered[points[0]]
                value = int.from_bytes(source[:8], "little")
                altered[points[0]] = (height, struct.pack("<Q", value + 1) + source[8:])
                control = ask(proc, raw, altered, parent)
                require(
                    all(
                        not control[context]
                        and "signature" in control.get(context + "_error", "").lower()
                        for context in ("policy", "consensus")
                    ),
                    "changed funding amount did not invalidate the signed spend",
                )
            for i, (value, script) in enumerate(outputs):
                coins[tid, i] = coin(parent + 1, value, script)
        proc.stdin.close()
        require(proc.wait() == 0, "GoBDK reader failed to finish")
    summary = {
        "kind": "confirmed-warmup-spend"
        if confirmed
        else "first-prospective-unsigned-candidate",
        "native_variant": native,
        "build": build,
        "go_oracle_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "parent_height": parent,
        "containing_height": parent + 1,
        "transactions": len(raws),
        "ordinary_policy_accepted": policy_count,
        "ordinary_consensus_accepted": consensus_count,
        "confiscations_requiring_signed_authorization": skipped,
        "ordinary_rejections": disagreements,
        "ordinary_consensus_complete": consensus_count == len(raws) - skipped,
        "ordinary_policy_complete": policy_count == len(raws) - skipped,
    }
    if confirmed:
        summary["altered_funding_amount"] = control
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("evidence", type=Path)
    p.add_argument("--go-oracle", required=True, type=Path)
    p.add_argument("--native", choices=("source", "distributed"), default="source")
    args = p.parse_args()
    result = replay(load(args.evidence), args.go_oracle.resolve(), args.native)
    print(json.dumps(result, indent=2))
    ok = result["ordinary_consensus_complete"] and (
        result["kind"] == "confirmed-warmup-spend" or result["ordinary_policy_complete"]
    )
    raise SystemExit(0 if ok else 1)
