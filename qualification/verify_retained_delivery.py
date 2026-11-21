"""Recalculate block identities, lineage and the delivered body/subtree commitments."""

import argparse
import hashlib
import json
from pathlib import Path


def dsha(raw):
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()


def verify(result):
    headers = [bytes.fromhex(h) for h in result["ordered_header_hex"]]
    ids = [dsha(h)[::-1].hex() for h in headers]
    assert len(headers) == len(set(ids)) == 5 and ids == result["ordered_block_ids"]
    assert ids[1:3] == result["local_solutions"]
    checks = result["single_sv_readbacks"]
    assert len(checks) == 2
    for idx, check in enumerate(checks, 1):
        assert check["parent"] == ids[idx - 1]
        assert (
            check["solution"] == check["stock_tip"] == check["entrant_tip"] == ids[idx]
        )
        assert check["peer_tip_reports"] == 1
        assert (
            check["queued_at_ms"]
            <= check["sent_at_ms"]
            <= check["requested_active_headers_at_ms"]
        )
        assert (
            check["requested_active_headers_at_ms"]
            <= check["inclusion_at_ms"]
            <= check["received_body_at_ms"]
        )
        assert check["chain_state_before_successor"] == "observed_active"
        assert check["settlement_before_successor"] is None
        assert check["before_readback"] == {
            "observation_window_ready": True,
            "solved_on_selected_parent": True,
            "job_preconditions_met": False,
            "job_wait_reason": "awaiting independent observation after solving on the selected parent",
        }
        assert check["after_readback"] == {
            "solved_on_selected_parent": False,
            "job_preconditions_met": False,
            "job_wait_reason": "no eligible pending transactions",
        }
    for header in headers:
        assert len(header) == 80 and header[72:76] == bytes.fromhex("ffff7f20")
        assert int.from_bytes(dsha(header), "little") <= 0x7FFFFF << (8 * 29)
    for header, parent in zip(headers[1:], ids):
        assert header[4:36][::-1].hex() == parent
    raw = bytes.fromhex(result["reference_block_hex"])
    payment, second = [bytes.fromhex(p) for p in result["payments_hex"]]
    assert payment and second and payment != second
    assert raw[:80] == headers[1] and raw[80] == 2 and raw.endswith(payment)
    assert dsha(dsha(raw[81 : -len(payment)]) + dsha(payment)) == headers[1][36:68]
    leaves = b"\xff" * 32 + dsha(payment)
    assert leaves.hex() == result["subtree_leaves_hex"]
    assert dsha(leaves)[::-1].hex() == result["subtree_root"]
    sha = hashlib.sha256(raw).hexdigest()
    assert len(raw) == result["reference_bytes"]
    for key in (
        "reference_sha256",
        "sv_getdata_after_restart_sha256",
        "asset_wire_after_subtree_first_sha256",
    ):
        assert result[key] == sha
    assert (
        result["retrieving_peer_sent_body_at_ms"] > 0
        and result["retrieving_peer_reported_tip"] is False
    )
    assert result["prior_chain_state"] == "settled"
    assert (
        result["foreign_successor_asset_status"] == 404
        and result["foreign_successor_sv_reply"] == "notfound"
    )
    assert result["invalidation_sequence"] == 1
    assert result["after_invalidation_sv_replies"] == ["notfound", "notfound"]
    assert result["after_invalidation_asset_status"] == {
        "block_legacy/" + ids[1] + "?wire=1": 404,
        "subtree/" + result["subtree_root"]: 404,
        "block/" + ids[2]: 404,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    verify(json.loads(parser.parse_args().receipt.read_text()))
    print("Verified retained block, subtree, successor links and delivery outcomes")
