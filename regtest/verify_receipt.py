"""Verify the recorded two-Teranode regtest proof using Python's standard library."""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

from prove import decode_block, dsha, parse_tx, read_varint, txid


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_header(header, expected_hash, previous=None):
    require(len(header) == 80 and txid(header) == expected_hash, "header hash mismatch")
    if previous is not None:
        require(header[4:36][::-1].hex() == previous, "broken previous-block link")
    require(header[72:76] == bytes.fromhex("ffff7f20"), "unexpected regtest compact target")
    require(int.from_bytes(dsha(header), "little") <= 0x7fffff << 232, "insufficient proof of work")


def single_input(raw):
    count, offset = read_varint(raw, 4)
    require(count == 1, "expected one transaction input in this fixture")
    previous = raw[offset:offset + 32][::-1].hex()
    index = struct.unpack_from("<I", raw, offset + 32)[0]
    size, start = read_varint(raw, offset + 36)
    return previous, index, raw[start:start + size]


def coinbase_height(script):
    if 0x51 <= script[0] <= 0x60:
        return script[0] - 0x50
    size = script[0]
    require(1 <= size <= 5 and len(script) > size and script[size] < 128, "invalid BIP34 height")
    return int.from_bytes(script[1:size + 1], "little")


def verify(record):
    require(record["network"] == "regtest", "expected a regtest receipt")
    funding_raw = bytes.fromhex(record["funding_transaction_hex"])
    funding_block = bytes.fromhex(record["funding_block_hex"])
    check_header(funding_block[:80], record["funding_block"], record["entrant_join_tip"])
    _, funding_txs, _ = decode_block(funding_block, record["funding_block"])
    require(funding_raw in funding_txs[1:] and txid(funding_raw) == record["funding_tx"],
            "funding transaction not committed by funding block")
    funding_previous, funding_index, _ = single_input(funding_raw)
    require((funding_previous, funding_index) == (record["funding_coinbase"], 0), "funding source mismatch")
    check_header(bytes.fromhex(record["parent_header_hex"]), record["parent"], record["funding_block"])

    candidate = bytes.fromhex(record["candidate_block_hex"])
    require(candidate[:80] == bytes.fromhex(record["solved_header"]), "candidate differs from solved header")
    check_header(candidate[:80], record["solved_hash"], record["parent"])
    _, txs, outputs = decode_block(candidate, record["solved_hash"])
    child = bytes.fromhex(record["child_transaction_hex"])
    require(len(txs) == 2 and txs[1] == child and txid(child) == record["child"], "candidate child mismatch")
    source, index, _ = single_input(child)
    require((source, index) == (record["funding_tx"], record["funding_output_index"]), "child outpoint mismatch")
    _, funding_outputs, _ = parse_tx(funding_raw, 0)
    input_value = funding_outputs[index][0]
    require(input_value == record["funding_output_value_satoshis"], "funding output value mismatch")
    fee = input_value - sum(value for value, _ in outputs[1])
    require(fee > 0 and fee == record["child_fee_satoshis"], "child fee mismatch")

    source, index, script = single_input(txs[0])
    require(source == "00" * 32 and index == 0xffffffff and 2 <= len(script) <= 100, "invalid coinbase input")
    height = coinbase_height(script)
    require(height == record["entrant_join_height"] + 3, "candidate height disagrees with join height")
    subsidy = 5_000_000_000 >> (height // 150)
    require(sum(value for value, _ in outputs[0]) == subsidy + fee, "coinbase reward mismatch")

    digest = hashlib.sha256(candidate).hexdigest()
    receipts = record["independent_receipts"]
    require(len(receipts) == 2 and {r["node"] for r in receipts} == {"tn1", "tn2"}, "two named receipts required")
    for r in receipts:
        require((r["height"], r["hash"], r["body_sha256"], r["transaction_count"]) ==
                (height, record["solved_hash"], digest, len(txs)), f"body receipt mismatch for {r['node']}")
    check_header(bytes.fromhex(record["successor_header_hex"]), record["successor"], record["solved_hash"])
    return height, fee


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path, help="JSON file saved by regtest/prove.sh")
    args = parser.parse_args()
    try:
        receipt = json.loads(args.receipt.read_text())
        height, fee = verify(receipt)
    except (OSError, KeyError, IndexError, TypeError, ValueError, struct.error) as error:
        sys.exit(f"Receipt verification failed: {error}")
    print(f"Verified regtest block {receipt['solved_hash']} at height {height}, child fee {fee} satoshis.\n"
          "Funding inclusion, header links, proof of work, Merkle commitments, coinbase reward, and both recorded body digests match.")
