"""Replay the stock Teranode notice, freeze, unfreeze and ordinary-spend receipt."""

import argparse
import json
import struct
import sys
from pathlib import Path

from ecdsa import SECP256k1, VerifyingKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import decode_block, dsha, txid
from verify_receipt import check_header, single_input


def message_digest(payload):
    text = payload.hex().encode()
    length = (
        bytes([len(text)])
        if len(text) < 253
        else b"\xfd" + struct.pack("<H", len(text))
    )
    return dsha(b"\x18Bitcoin Signed Message:\n" + length + text)


def verify_funding(record):
    if len(bytes.fromhex(record["node_binary_sha256"])) != 32:
        raise ValueError("missing release binary digest")
    history = record["history_from_height_one"]
    if len(history) != record["parent"]["height"]:
        raise ValueError("common funding chain length differs")
    previous = "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"
    funding_seen = False
    for raw in history:
        body = bytes.fromhex(raw)
        h = txid(body[:80])
        check_header(body[:80], h, previous)
        _, txs, outputs = decode_block(body, h)
        for tx, out in zip(txs, outputs, strict=True):
            if txid(tx) == record["funding_outpoint"]["txid"]:
                assert out[record["funding_outpoint"]["vout"]][0] > 0
                funding_seen = True
        previous = h
    if previous != record["parent"]["hash"] or not funding_seen:
        raise ValueError("missing funded output on the common parent")


def verify(record, *, full_history=True):
    rows = record["signed_records"]
    keys = set(record["genesis_public_keys"])
    if len(keys) != 5 or len(rows) != 3:
        raise ValueError("unexpected signing set or signed prefix")
    for index, row in enumerate(rows, 1):
        raw = bytes.fromhex(row["raw"])
        payload, compact = raw[:-195], raw[-195:]
        version, seq, _timestamp, kind = struct.unpack("<IIQI", payload[:20])
        if (
            (version, seq, kind) != (1, index, index)
            or row["sequence_number"] != index
            or not row["processed"]
        ):
            raise ValueError("signed prefix differs from stock application receipt")
        signers = set()
        for off in range(0, len(compact), 65):
            header = compact[off] - 27
            if not 4 <= header <= 5:
                raise ValueError(
                    "unexpected signature recovery header for compressed lab keys"
                )
            recovered = VerifyingKey.from_public_key_recovery_with_digest(
                compact[off + 1 : off + 65], message_digest(payload), curve=SECP256k1
            )
            signers.add(recovered[header & 1].to_string("compressed").hex())
        if len(signers) != 3 or not signers <= keys:
            raise ValueError("three distinct lab signers required")
        if kind == 1:
            notice = b"isolated positive interoperability observation"
            if payload[20:] != bytes([len(notice)]) + notice:
                raise ValueError("unexpected informational message")
        if kind in (2, 3):
            out = record["funding_outpoint"]
            vout, start, stop, policy_expires = struct.unpack("<QQQB", payload[52:])
            h = record["parent"]["height"]
            if (payload[20:52].hex(), vout, policy_expires) != (
                out["txid"],
                out["vout"],
                1,
            ):
                raise ValueError("funding outpoint differs from signed directive")
            if (start, stop) != ((h, 2**31 - 1) if kind == 2 else (h - 2, h - 1)):
                raise ValueError("funding interval differs")
    if full_history:
        verify_funding(record)
    spend = bytes.fromhex(record["ordinary_spend_hex"])
    ids = record["released_txids"]
    if (
        set(ids) != {"scary_node", "teranode", "sv_node"}
        or set(ids.values()) != {txid(spend)}
        or txid(spend) != record["ordinary_spend_txid"]
    ):
        raise ValueError("reference transaction IDs differ")
    reasons = record["frozen_rejections"]
    if set(reasons) != set(ids) or any(
        not any(term in text.lower() for term in ("frozen", "blacklist", "freeze"))
        for text in reasons.values()
    ):
        raise ValueError("missing frozen rejection")
    input_txid, input_index, _ = single_input(spend)
    if (input_txid, input_index) != (
        record["funding_outpoint"]["txid"],
        record["funding_outpoint"]["vout"],
    ):
        raise ValueError("ordinary payment does not spend the tested output")
    block = bytes.fromhex(record["block_hex"])
    check_header(block[:80], record["active_inclusion_hash"], record["parent"]["hash"])
    _, txs, _ = decode_block(block, record["active_inclusion_hash"])
    if txs.count(spend) != 1:
        raise ValueError("mined payment differs")
    if set(record["active_tips"]) != {
        "original_teranode",
        "second_teranode",
        "sv_node",
        "scary_node",
    } or set(record["active_tips"].values()) != {record["active_inclusion_hash"]}:
        raise ValueError("active-chain observations disagree")
    if (
        record["maintained_receiver_sequence"] != 3
        or record["maintained_receiver_unprocessed"] != 0
    ):
        raise ValueError("maintained receiver disposition differs")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument(
        "--outcomes-only",
        action="store_true",
        help="Verify the earlier receipt whose predecessor bodies were not retained.",
    )
    args = p.parse_args()
    record = json.loads(args.receipt.read_text())
    verify(record, full_history=not args.outcomes_only)
    print(
        "Verified three signed and applied records, frozen rejections, identical released payment and block commitment."
    )
    if not args.outcomes_only:
        print(
            f"Verified {len(record['history_from_height_one'])} common-parent blocks from regtest genesis."
        )
