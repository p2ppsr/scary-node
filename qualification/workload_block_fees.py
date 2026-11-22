"""Verify the paid fees and the coinbase's claim in every selected workload block."""

import argparse
import hashlib
import json
from array import array
from collections import Counter
from pathlib import Path

from recover_sustained_chain import read
from verify_sustained_native import (
    COHORTS,
    FUNDING,
    PAYMENTS,
    REORDERED_ARMS,
    cohort_by_batch,
    decode_block,
    dsha,
    read_source,
    require,
    verify,
    verify_block_membership,
)


def check(record, corpus, blocks):
    summary = verify(record)
    funding = record.get("setup", {}).get("funding", record["baseline"])
    funding_wire = (corpus / "funding.block").read_bytes()
    require(
        funding["hash"] == FUNDING
        and hashlib.sha256(funding_wire).hexdigest() == funding["wire_sha256"],
        "different signed funding bytes",
    )
    _, funding_txs, funding_outputs = decode_block(funding_wire, FUNDING)
    require(
        len(funding_txs) == 16_385
        and all(len(out) == 4 for out in funding_outputs[1:]),
        "unexpected split outputs",
    )
    initial = [value for out in funding_outputs[1:] for value, _ in out]
    ordinals = read_source(
        corpus, record["source"], funding=[dsha(tx) for tx in funding_txs[1:]]
    )
    arm = record.get("arm")
    cohorts = cohort_by_batch(arm) if arm in REORDERED_ARMS else None
    amounts = array("Q", [0]) * PAYMENTS
    created = array("I", [0]) * PAYMENTS
    seen = bytearray(PAYMENTS)
    fee_counts = Counter()
    rows = []
    for block in record["blocks"]:
        raw = (blocks / (block["hash"] + ".block")).read_bytes()
        txs, outputs = verify_block_membership(
            raw, block, ordinals, seen, PAYMENTS // COHORTS, source_cohorts=cohorts
        )
        fees = 0
        parent_ages = Counter()
        for tx, out in zip(txs[1:], outputs[1:], strict=True):
            index = ordinals[dsha(tx)]
            require(len(out) == 1, "ordinary payment has multiple outputs")
            parent = index - len(initial)
            if parent < 0:
                parent_height, input_value = funding["height"], initial[index]
            else:
                parent_height, input_value = created[parent], amounts[parent]
            value = out[0][0]
            require(
                0 < parent_height <= block["height"] and input_value >= value,
                "parent value or height is unavailable",
            )
            created[index], amounts[index] = block["height"], value
            fee = input_value - value
            parent_ages[block["height"] - parent_height] += 1
            fee_counts[fee] += 1
            fees += fee
        coinbase_value = sum(value for value, _ in outputs[0])
        subsidy = 5_000_000_000 >> (block["height"] // 150)
        require(
            coinbase_value <= subsidy + fees, "coinbase overclaims fees and subsidy"
        )
        rows.append(
            {
                "height": block["height"],
                "hash": block["hash"],
                "coinbase_txid": dsha(txs[0])[::-1].hex(),
                "coinbase_wire_hex": txs[0].hex(),
                "payments": len(txs) - 1,
                "transaction_fees_satoshis": fees,
                "subsidy_satoshis": subsidy,
                "coinbase_value_satoshis": coinbase_value,
                "unclaimed_satoshis": subsidy + fees - coinbase_value,
                "payment_parent_age_in_blocks": dict(sorted(parent_ages.items())),
            }
        )
    require(sum(seen) == summary["distinct_confirmed"], "different distinct payments")
    return {
        "arm": arm,
        "registration": record["registration"],
        "verified_funding_hash": FUNDING,
        "source_envelopes_checked": len(ordinals),
        "confirmed_distinct_payments": sum(seen),
        "payment_counts_by_fee_satoshis": dict(sorted(fee_counts.items())),
        "blocks": rows,
    }


def economics(record, proof, ledger):
    from prove import parse_tx
    from verify_receipt import coinbase_height, single_input
    from workload_solved_ledger import audit

    selected = audit(record, ledger)["selected"]
    require(
        proof["registration"] == record["registration"]
        and len(proof["blocks"]) == len(selected),
        "fee receipt differs",
    )
    totals = {}
    nonces = set()
    identity = bytes.fromhex(ledger["mining_identity"])
    require(len(identity) == 32, "invalid checkpoint identity")
    payout = bytes.fromhex("76a914751e76e8199196d454941c45d1b3a323f1433bd688ac")
    tag = b"/scary-node/"
    suffix = bytes([len(tag)]) + tag + bytes([len(identity)]) + identity
    for origin, row in zip(selected, proof["blocks"], strict=True):
        require(
            (row["height"], row["hash"], row["payments"])
            == (origin["height"], origin["hash"], origin["payments"]),
            "selected block or payment count differs",
        )
        tx = bytes.fromhex(row["coinbase_wire_hex"])
        raw, outputs, end = parse_tx(tx, 0)
        require(
            raw == tx
            and end == len(tx)
            and dsha(tx)[::-1].hex() == row["coinbase_txid"],
            "coinbase bytes and transaction ID disagree",
        )
        previous, index, script = single_input(tx)
        require(
            previous == "00" * 32
            and index == 0xFFFFFFFF
            and coinbase_height(script) == row["height"],
            "unexpected coinbase outpoint or height",
        )
        if origin["in_solved_journal"]:
            # The fixed workload heights use a pushed little-endian script number.
            pos = 1 + script[0]
            require(
                script[pos : pos + 1] == bytes([8])
                and script[pos + 9 :] == suffix
                and len(outputs) == 1
                and outputs[0][1] == payout,
                "ledger match has a different miner domain, extranonce encoding or payout",
            )
            nonces.add(script[pos + 1 : pos + 9])
        elif origin["owner"] == "sv":
            require(
                not script.endswith(suffix),
                "stock slot appears to bear the entrant's mining identity",
            )
        subsidy = 5_000_000_000 >> (row["height"] // 150)
        paid = row["transaction_fees_satoshis"]
        claim = sum(value for value, _ in outputs)
        require(
            row["coinbase_value_satoshis"] == claim
            and row["subsidy_satoshis"] == subsidy
            and 0 <= row["unclaimed_satoshis"] == subsidy + paid - claim,
            "incorrect subsidy, coinbase amount or total fees",
        )
        miner = totals.setdefault(
            origin["owner"],
            {
                "blocks": 0,
                "payments": 0,
                "transaction_fees_satoshis": 0,
                "claimed_above_subsidy_satoshis": 0,
                "unclaimed_reward_satoshis": 0,
                "payment_parent_age_in_blocks": Counter(),
            },
        )
        miner["blocks"] += 1
        miner["payments"] += row["payments"]
        miner["transaction_fees_satoshis"] += paid
        miner["claimed_above_subsidy_satoshis"] += max(0, claim - subsidy)
        miner["unclaimed_reward_satoshis"] += subsidy + paid - claim
        ages = {int(k): v for k, v in row["payment_parent_age_in_blocks"].items()}
        require(
            all(age >= 0 and count > 0 for age, count in ages.items())
            and sum(ages.values()) == row["payments"],
            "parent age counts differ",
        )
        miner["payment_parent_age_in_blocks"].update(ages)
    return {
        "selected_by_owner": totals,
        "distinct_selected_scary_extranonces": len(nonces),
    }


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="action", required=True)
    raw = commands.add_parser("wires")
    for name in ("receipt", "corpus", "blocks"):
        raw.add_argument(name, type=Path)
    grouped = commands.add_parser("owners")
    for name in ("receipt", "fees", "ledger"):
        grouped.add_argument(name, type=Path)
    args = cli.parse_args()
    record = read(args.receipt)
    if args.action == "owners":
        result = economics(record, read(args.fees), read(args.ledger))
    else:
        result = check(record, args.corpus, args.blocks)
    print(json.dumps(result, indent=2))
