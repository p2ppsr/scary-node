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


def verify_p2pk_signature(funding, payment):
    from ecdsa import SECP256k1, VerifyingKey, util
    from verify_receipt import single_input

    previous, index, unlock = single_input(payment)
    _, outputs, end = parse_tx(funding, 0)
    assert end == len(funding) and previous == txid(funding)
    value, script = outputs[index]
    assert script[0] in (33, 65) and len(script) == script[0] + 2 and script[-1] == 0xAC
    assert unlock[0] == len(unlock) - 1 and unlock[-1] == 0x41
    sequence_pos = 42 + len(unlock)
    sequence = payment[sequence_pos : sequence_pos + 4]
    count, outputs_pos = read_varint(payment, sequence_pos + 4)
    assert count == 1
    outpoint = payment[5:41]
    preimage = (
        payment[:4]
        + dsha(outpoint)
        + dsha(sequence)
        + outpoint
        + bytes([len(script)])
        + script
        + value.to_bytes(8, "little")
        + sequence
        + dsha(payment[outputs_pos:-4])
        + payment[-4:]
        + bytes([0x41, 0, 0, 0])
    )
    assert VerifyingKey.from_string(script[1:-1], curve=SECP256k1).verify_digest(
        unlock[1:-1], dsha(preimage), sigdecode=util.sigdecode_der
    )


def verify_early_offer(row, funding, payment, creating):
    early = row["early_offer"]
    parent_raw, body, _ = decode_block(
        bytes.fromhex(early["empty_successor_wire"]), row["parent"]
    )
    assert parent_raw[:80].hex() == row["parent_header"] and len(body) == 1
    before, seen, successor = [
        early[k] for k in ("before_offer", "after_first_offer", "after_successor")
    ]
    assert before["tip_hash"] == seen["tip_hash"] == creating
    assert (
        successor["tip_hash"] == row["parent"]
        and successor["height"] == seen["height"] + 1
    )
    assert all(s["observation_window_ready"] for s in (before, seen, successor))
    assert not before["job_preconditions_met"] and not seen["job_preconditions_met"]
    assert before["pending_transactions"] == seen["pending_transactions"] == 0
    assert (
        early["before_successor_worker"]["authorized"]
        and early["before_successor_worker"]["notifications"] == 0
    )
    assert early["stock_mempool_after_successor"] == [row["payment_txid"]]
    assert (
        early["stock_tx_bytes_after_successor"]
        == early["stock_tx_bytes_after_offer"]
        == early["stock_tx_bytes_before"] + 24 + len(payment)
    )
    assert row["funding_txid"] == txid(funding)
    verify_p2pk_signature(funding, payment)
    stats = [s["peer_transaction_intake"]["sv"] for s in (before, seen, successor)]
    assert all(
        s["peer_transaction_intake"]["teranode_subtree"]
        == before["peer_transaction_intake"]["teranode_subtree"]
        for s in (seen, successor)
    )
    if row["result"] == "withheld":
        assert (
            stats[1]["ineligible"] == stats[0]["ineligible"] + 1
            and stats[2] == stats[1]
        )
        assert (
            not successor["job_preconditions_met"]
            and successor["pending_transactions"] == 0
        )
        assert (
            early["after_successor_worker"]["authorized"]
            and early["after_successor_worker"]["notifications"] == 0
        )
        return
    assert row["result"] == "selected"
    assert seen["waiting_for_successor"] == 1 and seen[
        "waiting_for_successor_bytes"
    ] == len(payment)
    assert stats[1]["awaiting_successor"] == stats[0]["awaiting_successor"] + 1
    assert (
        stats[2]["promoted_after_successor"] == stats[0]["promoted_after_successor"] + 1
    )
    assert stats[2]["admitted"] == stats[0]["admitted"] + 1
    assert successor["pending_transactions"] == 1 and successor["job_preconditions_met"]
    assert (
        successor["waiting_for_successor"]
        == successor["waiting_for_successor_bytes"]
        == 0
    )
    assert (
        early["stock_tx_bytes_after_selection"] == early["stock_tx_bytes_after_offer"]
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
    if "early_offer" in row:
        verify_early_offer(row, funding, payment, creating)
        if row["result"] == "withheld":
            return {
                "source": source,
                "parent": row["parent"],
                "result": "withheld",
                "payment_txid": row["payment_txid"],
            }
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
        from verify_dar_history import verify_owner_signature

        verify_owner_signature(payment, *source_outputs[vout])
        assert row["subtree_hash"] in (payment_id, txid(b"\xff" * 32 + dsha(payment)))
        payload = bytes.fromhex(row["subtree_data_hex"])
        if "subtree_completion" in row:
            completion = row["subtree_completion"]
            assert completion["event"] == {
                "event": "subtree_processed",
                "hash": row["subtree_hash"],
                "transactions": 1,
                "bytes": len(payload),
            }
            assert (
                1 <= completion["workers"] <= 16
                and 1 <= completion["waiting_capacity"] <= 4096
            )
            assert (
                completion["successful_root_completions"] >= 1
                and completion["failed_root_completions"] >= 0
            )
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
    if "reply_loss" in row:
        from workload_worker_headers import prepared_header

        exchange = row["reply_loss"]
        slot = exchange["slot"]
        candidate = prepared_header(slot)
        assert slot["outcome"] == "error" and slot["error"] == "Stratum session closed"
        assert slot["parent"] == row["parent"] and candidate["header"] == raw[:80].hex()
        assert candidate["hash"] == row["block_hash"]
        submission, reply = exchange["forwarded_submit"], exchange["upstream_reply"]
        assert (
            submission["method"] == "mining.submit" and reply["id"] == submission["id"]
        )
        assert reply["result"] is True and reply["error"] is None
        assert submission["params"][1:] == [
            candidate["notify"][0],
            candidate["extranonce2"],
            candidate["notify"][7],
            raw[76:80][::-1].hex(),
        ]
        verify_p2pk_signature(funding, payment)
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
