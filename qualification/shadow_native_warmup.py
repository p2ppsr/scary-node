"""Demonstrate native fee reconstruction on a confirmed spend during observer warm-up."""

import argparse
import gzip
import json
from pathlib import Path

from shadow_native import (
    checked_header,
    inputs,
    proofs,
    require,
    txid,
    unpack_state,
    verify_fees,
    verify_funding,
    wire_body,
)


def capture(path):
    from prove import decode_block

    with path.open("rb") as handle:
        state = unpack_state(handle)
    blocks = {}
    selected = {}
    for applied in state["chain"]["applied"]:
        raw = wire_body(applied)
        h = txid(raw[:80])
        height = applied["height"]
        _, txs, _ = decode_block(raw, h)
        blocks[height] = (h, raw)
        for i, tx in enumerate(txs):
            tid = txid(tx)
            points, _ = inputs(tx)
            if i and all(k[0] in selected and selected[k[0]] < height for k in points):
                required = {}
                for source, _ in points:
                    required.setdefault(selected[source], set()).add(source)
                required.setdefault(height, set()).add(tid)
                return {
                    "kind": "confirmed-warmup-spend",
                    "tip_height": state["height"],
                    "spend": tid,
                    "window": [
                        {"height": n, "header": b[:80].hex()}
                        for n, (_, b) in blocks.items()
                        if min(required) <= n <= height
                    ],
                    "proofs": [
                        proofs(blocks[n][1], n, blocks[n][0], wanted)
                        for n, wanted in sorted(required.items())
                    ],
                }
            selected[tid] = height
    raise ValueError(
        "retained native bodies contain no cross-block spend with complete source history"
    )


def verify(row):
    require(row["kind"] == "confirmed-warmup-spend", "unexpected evidence")
    headers = {}
    last = None
    for r in row["window"]:
        h = checked_header(bytes.fromhex(r["header"]), r["height"])
        if last is not None:
            require(
                h["previous"] == last["hash"] and h["height"] == last["height"] + 1,
                "native chain discontinuity",
            )
        headers[r["height"]] = h["hash"]
        last = h
    previous = {}
    transactions = {}
    height = None
    for proof in row["proofs"]:
        outs = verify_funding(proof, headers[proof["height"]])
        for tid, txouts in outs.items():
            transactions[tid] = proof["transactions"][tid]["tx"]
            if tid == row["spend"]:
                height = proof["height"]
            for i, (value, _) in enumerate(txouts):
                previous[(tid, i)] = (proof["height"], value)
    payment = transactions[row["spend"]]
    points, _ = inputs(bytes.fromhex(payment))
    require(
        all(previous[p][0] < height for p in points),
        "source is not an earlier observed block",
    )
    fees = verify_fees(
        [payment], {f"{p[0]}:{p[1]}": {"value": previous[p][1]} for p in points}
    )
    return {
        "kind": row["kind"],
        "input_count": len(points),
        "spend_height": height,
        "payment_txid": row["spend"],
        "source_heights": sorted({previous[p][0] for p in points}),
        "fee_satoshis": fees[0],
        "native_proofs": sum(len(p["transactions"]) for p in row["proofs"]),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("evidence", type=Path)
    p.add_argument(
        "--capture", type=Path, help="read a committed mainnet witness.state file"
    )
    a = p.parse_args()
    if a.capture:
        row = capture(a.capture)
        result = verify(row)
        a.evidence.write_bytes(
            gzip.compress(
                json.dumps(row, sort_keys=True, separators=(",", ":")).encode(), mtime=0
            )
        )
    else:
        result = verify(json.loads(gzip.decompress(a.evidence.read_bytes())))
    print(json.dumps(result, indent=2))
