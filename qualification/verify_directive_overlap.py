"""Replay signed freeze, solutions, overlapping ingress, and independent stock dispositions."""

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import decode_block, txid
from verify_peer_intake import verify_p2pk_signature
from verify_receipt import check_header, single_input
from verify_teranode_alerts import require_lab_signers


def block(wire, parent=None):
    raw = bytes.fromhex(wire)
    h = txid(raw[:80])
    check_header(raw[:80], h, parent)
    _, txs, outs = decode_block(raw, h)
    return h, txs, outs, raw[4:36][::-1].hex()


def verify(row):
    mode = row["mode"]
    assert mode in ("sequence", "signed")
    t = row["timing_ns"]
    prefix = max(t["stratum_prefix_flushed"], t[mode + "_prefix_flushed"])
    end = min(t["stratum_tail_start"], t[mode + "_tail_start"])
    assert (
        t["stratum_write_start"] <= t["stratum_prefix_flushed"] <= t["record_available"]
    )
    assert (
        t["record_available"]
        <= t["sequence_write_start"]
        <= t["sequence_tail_flushed"]
        <= t["signed_write_start"]
    )
    assert (
        0 <= prefix < end
        and t["signed_tail_flushed"]
        <= t["durable_sequence_observed"]
        <= t["stock_freeze_applied"]
    )
    for channel in ("stratum", "sequence", "signed"):
        n = row["ingress_offsets"][channel]
        assert (
            0 < n["prefix_bytes"] < n["total_bytes"]
            and t[channel + "_prefix_flushed"] <= t[channel + "_tail_start"]
        )
    assert (
        row["ingress_offsets"]["stratum"]["prefix_bytes"] + 1
        == row["ingress_offsets"]["stratum"]["total_bytes"]
    )
    funding_hash, txs, _, _ = block(row["creating_wire"])
    funding = next(tx for tx in txs if txid(tx) == row["funding_txid"])
    parent, _, _, _ = block(row["parent_wire"], funding_hash)
    candidate, transactions, outs, _ = block(row["candidate_wire"], parent)
    assert (
        parent == row["parent"]
        and candidate == row["candidate"]
        and len(transactions) == 2
    )
    payment = transactions[1]
    assert txid(payment) == row["payment_txid"] and outs[0] == [(5000001100, b"\x51")]
    source, index, _ = single_input(payment)
    assert source == row["funding_txid"] and index == 0
    verify_p2pk_signature(funding, payment)
    frame = bytes.fromhex(row["signed_freeze"])
    body = frame[:-195]
    version, seq, _, kind = struct.unpack("<IIQI", body[:20])
    assert (version, seq, kind) == (1, 1, 2)
    assert body[20:] == bytes.fromhex(source) + struct.pack(
        "<QQQB", index, row["height"], 2**31 - 1, 0
    )
    require_lab_signers(body, frame[-195:], set(row["public_keys"]))
    receipt = row["receipt"]
    assert row["durable_sequence"] == receipt["sequence"] == 1 and receipt["kind"] == 2
    assert receipt["signed_sha256"] == hashlib.sha256(frame).hexdigest()
    assert not row["stock_freeze_result"]["notProcessed"]
    assert row["stock_proposal_before_freeze"] is None
    initial = row["stock_before"]
    assert initial == {"active": parent, "candidate_confirmations": None}
    observed = [
        row["stock_before_local_rpc_freeze"],
        row["stock_after_local_rpc_freeze"],
    ]
    final = row["stock_final"]
    successor, _, _, published_parent = block(row["stock_successor_wire"])
    assert final["active"] == successor and published_parent in (parent, candidate)
    local = [s for s in row["solutions"] if s["hash"] == candidate]
    assert row["solved_blocks"] == len(local) and len(local) <= 1
    if row["worker"]["acknowledged"]:
        assert local and t["stratum_tail_flushed"] <= t["worker_result"]
    if published_parent == candidate or any(r["active"] == candidate for r in observed):
        assert local
        outcome = "stock selected the candidate"
    elif not local:
        assert not row["worker"]["acknowledged"]
        outcome = "worker submission rejected"
    else:
        sent = any("sent_body" in events for events in local[0]["deliveries"].values())
        if local[0]["chain_state"] == "directive_retired" and not sent:
            outcome = (
                "locally accepted and retired without recorded stock body delivery"
            )
        else:
            outcome = (
                "locally accepted, stock adoption unobserved in recorded readbacks"
            )
    return {
        "overlap": mode,
        "simultaneously_incomplete_ns": end - prefix,
        "durable_sequence": 1,
        "acknowledged": row["worker"]["acknowledged"],
        "outcome": outcome,
        "candidate": candidate,
        "stock_successor": successor,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--ordered", type=Path)
    a = p.parse_args()
    if not __debug__:
        raise RuntimeError("enable assertions to verify receipts")
    rows = json.loads(a.receipt.read_bytes())
    assert len(rows) == 2 and {r["mode"] for r in rows} == {"signed", "sequence"}
    assert len({r["binary_sha256"] for r in rows}) == 1
    if a.ordered:
        controls = json.loads(a.ordered.read_bytes())
        first, last = sorted(controls, key=lambda r: r["order"])
        assert (
            first["order"] == "directive-first"
            and first["reference_tip"] == first["parent"]
            and not first["solutions"]
        )
        assert (
            last["order"] == "solution-first"
            and last["reference_tip"] == last["candidate"]
            and last["solutions"]
        )
        assert all(
            c["durable_sequence"] == 1 and c["receipt"]["kind"] == 2 for c in controls
        )
    print(json.dumps([verify(r) for r in rows], indent=2))
