"""Replay a regtest stock-peer transaction intake receipt using its exact wire bytes."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import decode_block, dsha, parse_tx, read_varint, txid


def header(hex_bytes, block_hash, parent):
    raw = bytes.fromhex(hex_bytes)
    assert (
        len(raw) == 80 and raw[4:36][::-1].hex() == parent and txid(raw) == block_hash
    )
    compact = int.from_bytes(raw[72:76], "little")
    assert compact == 0x207FFFFF
    assert int.from_bytes(dsha(raw), "little") <= (compact & 0x7FFFFF) << (
        8 * ((compact >> 24) - 3)
    )


def verify(row):
    assert row["network"] == "regtest"
    source = "teranode_subtree" if "subtree_hash" in row else "sv"
    creating = row.get("creating_block") or row["witnessed_funding_block"]
    funding = bytes.fromhex(row["funding_hex"])
    payment = bytes.fromhex(row["payment_hex"])
    payment_id = txid(payment)
    assert payment_id == row["payment_txid"]
    creating_raw, txs, _ = decode_block(
        bytes.fromhex(row["creating_block_wire"]), creating
    )
    assert funding in txs[1:]
    header(creating_raw[:80].hex(), creating, creating_raw[4:36][::-1].hex())
    assert row["parent"] != creating
    header(row["parent_header"], row["parent"], creating)
    raw, txs, outputs = decode_block(
        bytes.fromhex(row["solved_block"]), row["block_hash"]
    )
    assert txs[1:] == [payment]
    assert raw[:80].hex() == (row.get("stratum_header") or row["header_hex"])
    if source == "sv":
        assert [txid(tx) for tx in txs] == row["block_transaction_ids"]
        assert row["funding_txid"] == txid(funding)
    header(raw[:80].hex(), row["block_hash"], row["parent"])
    successor = row.get("successor") or row["sv_successor"]
    successor_header = row.get("successor_header") or row["sv_successor_header"]
    header(successor_header, successor, row["block_hash"])
    before = row["peer_intake_before"]
    after = row["peer_intake_after"]
    assert after[source]["admitted"] - before[source]["admitted"] == 1
    for peer in before:
        if peer != source:
            assert after[peer] == before[peer]
    _, source_outputs, end = parse_tx(funding, 0)
    assert end == len(funding) and payment[4] == 1 and payment[5:37] == dsha(funding)
    vout = int.from_bytes(payment[37:41], "little")
    fee = source_outputs[vout][0] - sum(amount for amount, _ in outputs[1])
    height_start = 42
    width = txs[0][height_start]
    assert 1 <= width <= 5
    height = int.from_bytes(
        txs[0][height_start + 1 : height_start + 1 + width], "little"
    )
    subsidy = (50 * 10**8) >> (height // 150)
    assert fee > 0 and outputs[0] == [(subsidy + fee, b"\x51")]
    if source == "sv":
        assert (fee, subsidy, outputs[0][0][0]) == (
            row["fee_satoshis"],
            row["subsidy_satoshis"],
            row["payout_satoshis"],
        )
    if source == "teranode_subtree":
        assert row["subtree_hash"] in (payment_id, txid(b"\xff" * 32 + dsha(payment)))
        payload = bytes.fromhex(row["subtree_data_hex"])
        if payload[4:10] == b"\x00" * 5 + b"\xef":
            n, start = read_varint(payload, 10)
            assert n == 1
            size, script = read_varint(payload, start + 36)
            end = script + size + 4
            previous_size, src_script = read_varint(payload, end + 8)
            payload = (
                payload[:4]
                + bytes([n])
                + payload[start:end]
                + payload[src_script + previous_size :]
            )
        assert payload == payment
    return {
        "source": source,
        "parent": row["parent"],
        "fee_satoshis": fee,
        "payout_satoshis": outputs[0][0][0],
        "stock_active_block": row["block_hash"],
        "successor": successor,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipts", type=Path, nargs="+")
    args = p.parse_args()
    print(
        json.dumps(
            [verify(json.loads(path.read_text())) for path in args.receipts], indent=2
        )
    )
