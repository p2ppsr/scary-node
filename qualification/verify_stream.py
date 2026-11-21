"""Recompute multi-round Stratum block commitments, fees, active ancestry and independent background inclusion."""

import gzip
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import decode_block, txid
from verify_receipt import check_header, single_input


def verify(path):
    with gzip.open(path, "rt") as f:
        data = json.load(f)["stream"]
    rows = data["rounds"]
    planned = data.get("planned_rounds", 8)
    assert (
        planned,
        data["ordinary_payments_per_round"],
        data.get("admission_batch", 1),
        data.get("planned_background", 24),
    ) in {(8, 1024, 1, 24), (24, 2048, 256, 96)}
    assert len(rows) == planned
    ordinary = data["ordinary_payments_per_round"]
    funding = bytes.fromhex(data["funding_block_hex"])
    fund_hash = data["funding_block"]
    check_header(funding[:80], fund_hash)
    _, txs, outputs = decode_block(funding, fund_hash)
    ix = next(i for i, tx in enumerate(txs) if txid(tx) == data["funding"])
    balances = outputs[ix]
    assert len(balances) == len(rows) * (ordinary + 1)
    previous = fund_hash
    spent = set()
    landed = {}
    for k, row in enumerate(rows):
        p = bytes.fromhex(row["parent_header_hex"])
        check_header(p, row["parent"], previous if k == 0 else None)
        if k:
            assert row["parent"] == previous
        raw = bytes.fromhex(row["body_hex"])
        h = row["solved_hash"]
        check_header(raw[:80], h, row["parent"])
        _, txs, outputs = decode_block(raw, h)
        assert (
            len(txs) == row["transactions"] + 1 == ordinary + 2
            and len(raw) == row["wire_bytes"]
        )
        fee = 0
        large = 0
        for tx, produced in zip(txs[1:], outputs[1:]):
            fund, n, _ = single_input(tx)
            assert fund == data["funding"] and n not in spent and 0 <= n < len(balances)
            spent.add(n)
            fee += balances[n][0] - sum(v for v, _ in produced)
            for v, script in produced:
                if script.startswith(b"\x00\x6a\x4e"):
                    assert (
                        v == 0
                        and len(script[7:])
                        == int.from_bytes(script[3:7], "little")
                        == data["data_bytes_per_round"]
                    )
                    large += 1
        assert fee == row["fee_satoshis"] and large == 1
        assert (
            sum(v for v, _ in outputs[0])
            == (5000000000 >> (row["height"] // 150)) + fee
        )
        rs = row["receivers"]
        assert len(rs) == 3 and {r["receiver"] for r in rs} == {"sv", "tn1", "tn2"}
        assert all(
            r["hash"] == h and r["body_sha256"] == hashlib.sha256(raw).hexdigest()
            for r in rs
        )
        previous = h
        for encoded, expected in zip(
            row["successors_hex"], row["settlement"]["successors"], strict=True
        ):
            b = bytes.fromhex(encoded)
            check_header(b[:80], expected, previous)
            _, ts, _ = decode_block(b, expected)
            landed.update((txid(t), expected) for t in ts[1:])
            previous = expected
        assert (
            len(row["successors_hex"]) == 2
            and previous == row["settlement"]["settlement_tip"]
        )
    for encoded in data["final_bodies_hex"]:
        b = bytes.fromhex(encoded)
        h = txid(b[:80])
        check_header(b[:80], h, previous)
        _, ts, _ = decode_block(b, h)
        landed.update((txid(t), h) for t in ts[1:])
        previous = h
    offers = data["background"]
    assert len(spent) == len(balances)
    assert (
        len(offers)
        == len({o["txid"] for o in offers})
        == data.get("planned_background", 24)
    )
    assert all(
        landed[o["txid"]] == o["active_block"] and o["open_ns"] <= o["ack_ns"]
        for o in offers
    )
    assert all(
        o["open_ns"] >= i * data["background_cadence_ns"] for i, o in enumerate(offers)
    )
    return {
        "rounds": len(rows),
        "candidate_transactions": sum(r["transactions"] for r in rows),
        "candidate_wire_bytes": sum(r["wire_bytes"] for r in rows),
        "active_wire_matches": sum(len(r["receivers"]) for r in rows),
        "background_payments": len(offers),
        "background_during_rounds": sum(
            rows[0]["start_ns"] <= o["open_ns"] <= rows[-1]["settled_ns"]
            for o in offers
        ),
        "background_inclusion_blocks": len({o["active_block"] for o in offers}),
        "wall_seconds": data["total_wall_ns"] / 1e9,
    }


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1])), indent=2))
