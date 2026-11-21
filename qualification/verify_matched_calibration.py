"""Independently replay single-source native gate calibration and derive the timing proposal."""

import argparse
import hashlib
import json
import math
from pathlib import Path

from cohort_archive import check
from cohort_estimate import decode_block, dsha, txid
from matched_propagation import TARGET, gate_agent
from sv_reference import IMAGE


def verify(r):
    check(
        r["complete"]
        and r["reference_image"] == IMAGE
        and r["private_stock_bridges"] == 4,
        "incomplete native calibration",
    )
    check(
        r["clock"]["resolution_seconds"] > 0 and bool(r["clock"]["implementation"]),
        "missing host monotonic clock",
    )
    expected = ["scary" if i % 2 == 0 else "stock-right" for i in range(16)]
    check(
        r["planned_sources"] == expected and len(r["rows"]) == len(expected),
        "wrong single-producer plan",
    )
    previous = r["base"]
    nonces = set()
    max_i = max_native = 0
    for i, (row, source) in enumerate(zip(r["rows"], expected)):
        raw = bytes.fromhex(row["wire_hex"])
        h = row["solution"]["hash"]
        sha256 = hashlib.sha256(raw).hexdigest()
        _, txs, _ = decode_block(raw, h)
        check(
            row["index"] == i
            and row["source"] == source
            and row["parent"] == previous
            and raw[4:36][::-1].hex() == previous,
            "wrong calibration ancestry",
        )
        check(
            raw[72:76] == bytes.fromhex("ffff7f20")
            and int.from_bytes(dsha(raw[:80]), "little") <= TARGET
            and len(txs) == 2
            and txid(txs[1]) == row["payment_txid"],
            "invalid source work",
        )
        check(
            sha256 == row["solution"]["body_sha256"]
            and row["receiver_wire_sha256"] == [sha256] * 2
            and row["receiver_active_hashes"] == [h] * 2,
            "independent stock readbacks differ",
        )
        roster = row["peer_roster"]
        check(roster == row["peer_roster_after"] and len(roster) == 2, "roster changed")
        for idx, peers in enumerate(roster):
            check(
                [p["agent"] for p in peers]
                == [gate_agent(f"l{lane}-r{idx}") for lane in range(2)]
                and len({p["connection"] for p in peers}) == 2,
                "unexpected receiver peer",
            )
        captured = row["native_capture_ns"]
        max_native = max(max_native, captured)
        check(
            0 <= row["native_submit_ack_ns"] and captured >= 0, "invalid native timing"
        )
        legs = row["legs"]
        check(len(legs) == 2 and all(len(l) == 2 for l in legs), "missing receiver leg")
        due = legs[0][0]["planned_release_ns"]
        for lane in legs:
            for leg in lane:
                nonce = bytes.fromhex(leg["ping_nonce"])
                check(
                    len(nonce) == 8
                    and nonce not in nonces
                    and leg["ping_nonce"] == leg["pong_nonce"],
                    "missing or duplicated pong",
                )
                nonces.add(nonce)
                check(
                    captured
                    <= due
                    == leg["planned_release_ns"]
                    <= leg["write_begin_ns"]
                    <= leg["write_end_ns"]
                    <= leg["pong_ns"],
                    "invalid calibration timestamps",
                )
        bounds = [
            [
                legs[0][j]["write_begin_ns"] - legs[1][j]["pong_ns"],
                legs[0][j]["pong_ns"] - legs[1][j]["write_begin_ns"],
            ]
            for j in range(2)
        ]
        check(bounds == row["left_minus_right_bounds_ns"], "incorrect ingress bounds")
        max_i = max(max_i, max(abs(n) for pair in bounds for n in pair))
        previous = h
    tolerance = (math.ceil(max_i / 1e6) + 2) * 1_000_000
    hold = max(200_000_000, math.ceil(2 * max_native / 50_000_000) * 50_000_000)
    result = {
        "max_ingress_bound_ns": max_i,
        "max_native_capture_ns": max_native,
        "proposed_tolerance_ns": tolerance,
        "proposed_hold_ns": hold,
        "proposed_signed_delay_ns": max(40_000_000, 8 * tolerance),
    }
    check(result == r["calibrated"], "calibrated treatment differs")
    return result


def verify_continuation(r):
    check(
        r["complete"]
        and r["reference_image"] == IMAGE
        and r["private_stock_bridges"] == 4
        and r["planned_receivers"] == [0, 1]
        and len(r["rows"]) == 2,
        "incomplete unopposed native synchronization",
    )
    parent = r["base"]
    for i, row in enumerate(r["rows"]):
        nc = row["native_continuation"]
        check(
            row["parent"] == parent
            and row["plan"]["successor_receiver"] == nc["source_receiver"] == i,
            "wrong native continuation producer",
        )
        check(
            len(row["raw_hex"]) == 3
            and len(nc["body_sha256"]) == 3
            and nc["hashes"] == row["generated_hashes"],
            "unexpected native body list",
        )
        for j, encoded in enumerate(row["raw_hex"]):
            raw = bytes.fromhex(encoded)
            h = dsha(raw[:80])[::-1].hex()
            _, txs, _ = decode_block(raw, h)
            check(
                raw[4:36][::-1].hex() == parent
                and h == nc["hashes"][j]
                and hashlib.sha256(raw).hexdigest() == nc["body_sha256"][j]
                and int.from_bytes(dsha(raw[:80]), "little") <= TARGET
                and raw[72:76] == bytes.fromhex("ffff7f20"),
                "post-cutoff native transfer differs from verified wire",
            )
            if j == 0:
                check(
                    any(txid(tx) == row["payment_txid"] for tx in txs[1:]),
                    "scheduled signed payment missing",
                )
            parent = h
        check(
            nc["other_stock_active_tips"] == [parent] * 3
            and row["all_stock_active_hashes"] == [parent] * 4
            and row["receiver_roster"] == row["receiver_roster_after"],
            "stock tip or isolated roster differs",
        )
        for j, roster in enumerate(row["receiver_roster"]):
            check(
                [p["agent"] for p in roster]
                == [gate_agent(f"l{lane}-r{j}") for lane in range(2)]
                and len({p["connection"] for p in roster}) == 2,
                "unexpected native receiver peer",
            )
    return {"stock_nodes": 4, "transferred_blocks": 6, "last_stock_hash": parent}


if __name__ == "__main__":
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument("receipt", type=Path)
    receipt = json.loads(a.parse_args().receipt.read_text())
    verifier = (
        verify_continuation
        if receipt["kind"] == "unopposed continuation transport"
        else verify
    )
    print(json.dumps(verifier(receipt), indent=2))
