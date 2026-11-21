"""Replay deep-recovery wires, relative work, signed invalidation and the restored boundary."""

import argparse
import gzip
import hashlib
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import decode_block, dsha, txid
from sv_reference import IMAGE
from verify_receipt import check_header
from verify_teranode_alerts import require_lab_signers


def segment(wires, parent):
    hashes, work = [], 0
    for encoded in wires:
        raw = bytes.fromhex(encoded)
        h = txid(raw[:80])
        check_header(raw[:80], h, parent)
        decode_block(raw, h)
        bits = int.from_bytes(raw[72:76], "little")
        assert bits == 0x207FFFFF
        target = (bits & 0x7FFFFF) << (8 * ((bits >> 24) - 3))
        work += (1 << 256) // (target + 1)
        hashes.append(h)
        parent = h
    return hashes, work


def verify(case):
    signed = case["case"] == "signed"
    wide = case["case"] == "offline_fork"
    depth = 32 if wide else 12
    anchor = case["anchor"]
    anc = bytes.fromhex(case["anchor_wire"])
    assert txid(anc[:80]) == anchor
    decode_block(anc, anchor)
    assert case["source"] == IMAGE
    creation = bytes.fromhex(case["old_funding_wire"])
    creation_id = txid(creation[:80])
    check_header(anc[:80], anchor, creation_id)
    _, funding, _ = decode_block(creation, creation_id)
    assert case["old_funding_txid"] in [txid(tx) for tx in funding[1:]]
    old, work = segment(case["old_branch"][:depth], anchor)
    alt, alternative_work = segment(case["alternative_branch"], anchor)
    before, restart = case["before"], case["restart"]
    assert before["tip_hash"] == restart["tip_hash"] == old[-1]
    assert (
        0 < before["recoverable_blocks"] < depth and before["observation_window_ready"]
    )
    assert len(alt) == depth + 2 and not set(old).intersection(alt)
    assert (
        segment(case["alternative_branch"][:depth], anchor)[1]
        == work
        < alternative_work
    )
    if signed:
        tail, _ = segment(case["old_branch"][depth:], old[-1])
        assert len(tail) > 24 and case["unknown_offers"] == tail[-2:]
        for name in ("held", "restart", "held_with_unlinked_tips"):
            row = case[name]
            assert row["durable_directive_sequence"] == 1 and row["tip_hash"] == old[-1]
            assert (
                not row["observation_window_ready"] and not row["job_preconditions_met"]
            )
            assert (
                "directive invalidated ancestry beyond undo coverage"
                in row["readiness_reasons"]
            )
        assert case["held_with_unlinked_tips"]["buffered_bodies"] >= 2
        assert case["held_with_unlinked_tips"]["connected_data_peers"] > 0
        frame = bytes.fromhex(case["signed_frame"])
        payload = frame[:-195]
        version, sequence, _, kind = struct.unpack("<IIQI", payload[:20])
        assert (version, sequence, kind) == (1, 1, 7) and payload[
            20:52
        ] == bytes.fromhex(old[0])[::-1]
        require_lab_signers(payload, frame[-195:], set(case["lab_public_keys"]))
        receipt = case["application_receipt"]
        assert (
            receipt["sequence"] == 1
            and receipt["kind"] == 7
            and receipt["signed_sha256"] == hashlib.sha256(frame).hexdigest()
        )
    elif wide:
        assert not case["signed_frame"] and case["equal_work"] is None
        offline = case["offline_before_fork"]
        assert (
            offline["connected_data_peers"] == 0
            and not offline["job_preconditions_met"]
        )
        assert offline["tip_hash"] == old[-1]
    else:
        assert not case["signed_frame"] and case["equal_work"]["tip_hash"] == old[-1]
    rebuilt = case["recovered"]
    assert (
        rebuilt["tip_hash"] == alt[-1]
        and rebuilt["coverage_start"] == before["height"] - depth + 1
    )
    assert rebuilt["headers"] == len(alt) and rebuilt["pending_transactions"] == 0
    assert (
        rebuilt["observation_window_ready"]
        and case["old_funding_observed"]
        and not case["old_funding_after_reset"]
    )
    final_wires = [
        case[k]
        for k in ("funding_wire", "parent_wire", "solved_wire", "successor_wire")
    ]
    blocks, _ = segment(final_wires, alt[-1])
    _, funding, _ = decode_block(bytes.fromhex(final_wires[0]), blocks[0])
    _, txs, outs = decode_block(bytes.fromhex(final_wires[2]), blocks[2])
    assert len(txs) == 2 and txid(txs[1]) == case["payment_txid"] and txs[1][4] == 1
    source = txs[1][5:37]
    assert any(dsha(tx) == source for tx in funding[1:])
    source_index = next(i for i, tx in enumerate(funding) if dsha(tx) == source)
    funding_outputs = decode_block(bytes.fromhex(final_wires[0]), blocks[0])[2][
        source_index
    ]
    fee = funding_outputs[int.from_bytes(txs[1][37:41], "little")][0] - sum(
        n for n, _ in outs[1]
    )
    final = case["final"]
    subsidy = (50 * 10**8) >> ((final["height"] - 1) // 150)
    assert fee > 0 and outs[0] == [(subsidy + fee, b"\x51")]
    for row in (final, case["restored_after_reentry"]):
        assert row["tip_hash"] == blocks[-1] and row[
            "durable_directive_sequence"
        ] == int(signed)
        assert row["observation_window_ready"]
    return {
        "case": case["case"],
        "incumbent_suffix_work": work,
        "replacement_suffix_work": alternative_work,
        "recovered": alt[-1],
        "fee_satoshis": fee,
        "stock_selected": blocks[-2],
        "successor": blocks[-1],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    args = p.parse_args()
    encoded = args.receipt.read_bytes()
    if args.receipt.suffix == ".gz":
        encoded = gzip.decompress(encoded)
    result = json.loads(encoded)
    if not __debug__:
        raise RuntimeError("receipt verification requires enabled assertions")
    assert (
        len(bytes.fromhex(result["binary_sha256"])) == 32 and len(result["cases"]) == 3
    )
    assert {c["case"] for c in result["cases"]} == {
        "signed",
        "greater_work",
        "offline_fork",
    }
    print(json.dumps([verify(c) for c in result["cases"]], indent=2))
