"""Replay the native payments and stock-selected blocks in the controlled shutdown study."""

import argparse
import json
from pathlib import Path

from verify_peer_intake import (
    decode_block,
    header,
    parse_tx,
    txid,
    verify_p2pk_signature,
)
from verify_receipt import single_input


def verify(rows, baseline):
    assert [row["exit"]["signal"] for row in rows] == ["SIGINT", "SIGTERM"]
    assert len({row["release"] for row in rows}) == 1
    results = []
    for row in rows:
        funding = bytes.fromhex(row["funding_hex"])
        payment = bytes.fromhex(row["payment_hex"])
        verify_p2pk_signature(funding, payment)
        assert txid(payment) == row["payment_id"]
        if "before_waiting" in row:
            assert row["before_waiting"] == row["sender_waiting_events"] == 1
            assert row["before_pending"] == row["sender_native_admissions"] == 0
            assert row["restored_waiting"] == int(not baseline)
            assert row["restored_before_endorsement_pending"] == 0
            idle = row["pre_endorsement_worker"]
            assert idle["authorized"] and idle["notifications"] == 0
            intake = row["after_endorsement_intake"]
            assert (
                intake["sv"]["admitted"]
                == intake["sv"]["promoted_after_successor"]
                == int(not baseline)
            )
            assert all(n == 0 for n in intake["teranode_subtree"].values())
        else:
            assert row["before_pending"] == row["sender_native_admissions"] == 1
        assert row["new_peer_tx_bytes"] == 0
        funding_hash = txid(bytes.fromhex(row["funding_block_header"]))
        header(row["parent_header"], row["parent"], funding_hash)
        assert ("solved_block" in row) == (not baseline)
        if not baseline or "funding_block_hex" in row:
            _, contents, _ = decode_block(
                bytes.fromhex(row["funding_block_hex"]), funding_hash
            )
            assert funding in contents
        if "solved_block" in row:
            assert row["restored_pending"] == 1 and row["exit"]["exit_code"] == 0
            raw = bytes.fromhex(row["solved_block"])
            selected = txid(raw[:80])
            header(raw[:80].hex(), selected, row["parent"])
            _, txs, outputs = decode_block(raw, selected)
            assert txs[1:] == [payment]
            _, index, _ = single_input(payment)
            _, created, _ = parse_tx(funding, 0)
            fee = created[index][0] - sum(x[0] for x in outputs[1])
            assert fee > 0 and outputs[0] == [
                ((50 * 100_000_000 >> ((row["height"] + 1) // 150)) + fee, b"\x51")
            ]
            successor = bytes.fromhex(row["stock_successor_header"])
            header(successor.hex(), txid(successor), selected)
            results.append(
                {
                    "signal": row["exit"]["signal"],
                    "restored": True,
                    "selected": selected,
                    "successor": txid(successor),
                    "fee_satoshis": fee,
                }
            )
        else:
            assert row["restored_pending"] == 0
            assert row["worker"]["authorized"] and row["worker"]["notifications"] == 0
            results.append({"signal": row["exit"]["signal"], "restored": False})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            verify(json.loads(args.receipt.read_bytes()), args.baseline), indent=2
        )
    )
