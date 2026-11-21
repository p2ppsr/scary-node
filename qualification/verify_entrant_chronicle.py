"""Verify source outputs, context decisions, Merkle inclusion and independent stock adoption for the live entrant corpus."""

import argparse
import gzip
import json
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import decode_block, read_varint, txid
from verify_scenario import compact
from verify_receipt import check_header, require


def outputs(record):
    result = {}
    for h, b in record["funding_blocks"].items():
        raw = bytes.fromhex(b["body_hex"])
        check_header(raw[:80], h)
        _, txs, vouts = decode_block(raw, h)
        for tx, outs in zip(txs, vouts, strict=True):
            for i, (amount, locking) in enumerate(outs):
                result[(txid(tx), i)] = (
                    b["height"],
                    struct.pack("<Q", amount) + compact(len(locking)) + locking,
                )
    return result


def extend(raw, coins):
    inputs, start = read_varint(raw, 4)
    extended = bytearray(raw[:4] + bytes.fromhex("0000000000ef") + raw[4:start])
    heights = []
    for _ in range(inputs):
        h = raw[start : start + 32][::-1].hex()
        index = struct.unpack_from("<I", raw, start + 32)[0]
        size, pos = read_varint(raw, start + 36)
        end = pos + size + 4
        height, source = coins[(h, index)]
        heights.append(height)
        extended.extend(raw[start:end] + source)
        start = end
    return bytes(extended) + raw[start:], heights


def verify(record):
    require(record["schema"] == 1, "unexpected release corpus schema")
    build = record["interpreter"]
    require(
        build["bdk_commit"] == "8a4e1d3ddb16cbb2b3a98f82ea1ee6ff19a297e9"
        and build["sv_node_commit"] == "6504a3aff65ba97c0f6c80962b033e35ecbfed4b"
        and build["linked_bdk_version"] == "1.4.0"
        and build["linked_sv_version"] == "1.2.3",
        "running interpreter does not match the registered source",
    )
    require(
        record["reference"]
        == "bitcoinsv/bitcoin-sv:1.2.3@sha256:d96fe4786a00e5fc10bc9e0e76a6006376f9472d4eb74d07e4c1e0d019493d33",
        "unexpected stock SV image",
    )
    coins = outputs(record)
    rows = record["opcodes"]["rows"] + record["signatures"]
    require(
        len(record["opcodes"]["rows"]) == 11 and len(record["signatures"]) == 51,
        "incomplete pinned corpus",
    )
    policy = consensus = mined = checked = 0
    roles = {"rust", "go", "sv", "scary"}
    last = None
    disagreements = []
    for i, r in enumerate(rows):
        obs = r["observations"]
        require(set(obs) == roles, "missing independent observer")
        raw = bytes.fromhex(r.get("raw_tx", r.get("raw")))
        ef, heights = extend(raw, coins)
        require(
            txid(raw) == r["txid"]
            and ef.hex() == r.get("extended_hex", r.get("extended")),
            "extended input differs from witnessed funding",
        )
        require(
            len(set(heights)) == 1
            and heights[0] == r.get("utxo_height", r.get("source_height")),
            "input creation height differs",
        )
        s = obs["scary"]
        stock = obs["sv"]
        c = s["check"]
        require(
            s["coverage_from"]
            <= heights[0]
            <= s["parent_height"]
            == r.get("parent_height", r.get("tip")),
            "source outside observation boundary",
        )
        require(
            s["applied_sequence"] == 0,
            "unexpected directive prefix in clean vector lab",
        )
        require(
            c["outcome"] in ("determinate", "invalid")
            and not c["unknown_inputs"]
            and not c["script_checks_deferred"],
            "witnessed check unavailable",
        )
        require(
            s["consensus"] == (c["outcome"] == "determinate"),
            "consensus check label inconsistent",
        )
        for context in ("policy", "consensus"):
            if len({obs[role][context] for role in roles}) != 1:
                disagreements.append(
                    {
                        "row": i,
                        "txid": r["txid"],
                        "context": context,
                        "results": {role: obs[role][context] for role in sorted(roles)},
                    }
                )
        expected = (
            r["expected_consensus"]
            if "expected_consensus" in r
            else r["case"] != "false-result"
        )
        require(
            expected == stock["consensus"],
            "stock result differs from pinned expected outcome",
        )
        policy += stock["policy"]
        consensus += stock["consensus"]
        checked += c["script_checks_passed"]
        h = r.get("block_hash", r.get("block"))
        if stock["consensus"]:
            body = bytes.fromhex(s["active_body_hex"])
            check_header(body[:80], h, s["parent"])
            _, txs, _ = decode_block(body, h)
            require(
                txs[1:] == [raw] and s["stock_active"] == h,
                "stock active body differs from tested payment",
            )
            checks = s["block_checks"]
            require(
                checks["hash"] == h
                and checks["coinbase_reward_checked"]
                and checks["unknown_inputs"] == 0
                and checks["transactions_determinate"] == 1,
                "active payment did not receive complete witnessed checks",
            )
            last = h
        else:
            require(
                s["stock_active"] == s["parent"] and "active_body_hex" not in s,
                "rejected reference advanced the chain",
            )
        if s["origin"] == "scary-stratum":
            require(
                stock["consensus"]
                and stock["policy"]
                and s["consensus"]
                and s["policy"],
                "invalid entrant-originated selection",
            )
            mined += 1
        else:
            require(s["origin"] == "sv-reference", "unknown block producer")
    final = bytes.fromhex(record["final_body_hex"])
    require(last is not None, "no observed active corpus blocks")
    check_header(final[:80], record["final_successor"], last)
    decode_block(final, record["final_successor"])
    return {
        "cases": len(rows),
        "comparisons": 2 * len(rows),
        "stock_policy_acceptances": policy,
        "stock_consensus_acceptances": consensus,
        "scary_stratum_blocks_accepted_by_stock": mined,
        "positive_read_only_input_script_checks": checked,
        "disagreements": disagreements,
        "all_policy_acceptances_mined": mined == policy,
        "release_sha256": record["node_sha256"],
        "final_successor": record["final_successor"],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    a = p.parse_args()
    with gzip.open(a.receipt, "rt") as f:
        summary = verify(json.load(f))
    print(json.dumps(summary, indent=2))
    if summary["disagreements"] or not summary["all_policy_acceptances_mined"]:
        raise SystemExit(1)
