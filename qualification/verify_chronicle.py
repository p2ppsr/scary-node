"""Check the shipped result tables, solved bytes and independent peer outcomes."""

import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "regtest"))
from prove import decode_block, dsha, txid


def verify(directory):
    rows = json.loads((directory / "chronicle-opcodes.json").read_text())["rows"]
    with gzip.open(directory / "chronicle-signatures.json.gz", "rt") as file:
        rows += json.load(file)["rows"]
    for row in rows:
        for mode in ("policy", "consensus"):
            assert (
                len({row["observations"][name][mode] for name in ("rust", "go", "sv")})
                == 1
            )
        if "expected_consensus" in row:
            assert row["observations"]["sv"]["consensus"] == row["expected_consensus"]
    dar = json.loads((directory / "scary-dar-lifecycle.json").read_text())
    for block, raw, txn_id in [
        ("confiscation_block", "confiscation_block_hex", "confiscation_txid"),
        ("confirmed_hash", "recipient_block_hex", "recipient_spend"),
    ]:
        body = bytes.fromhex(dar[raw])
        assert dsha(body[:80])[::-1].hex() == dar[block]
        assert int.from_bytes(dsha(body[:80]), "little") <= 0x7FFFFF << 232
        _, txs, _ = decode_block(body, dar[block])
        assert len(txs) == 2 and txid(txs[1]) == dar[txn_id]
        assert dsha(dsha(txs[0]) + dsha(txs[1])) == body[36:68]
    assert dar["reorg_solutions"][-1]["chain_state"] == "stale_at_depth"
    assert all(
        item["chain_state"] == "settled" for item in dar["settlement_before_reorg"]
    )
    assert dar["scary_active_tip"] == dar["remined_spend_hash"]
    first, second = json.loads(
        (directory / "chronicle-directive-race.json").read_text()
    )
    assert (
        first["order"] == "directive-first"
        and first["reference_tip"] == first["parent"]
        and not first["solutions"]
    )
    assert (
        second["order"] == "solution-first"
        and second["reference_tip"] == second["candidate"]
    )
    for r in (first, second):
        assert (
            r["receipt"]["sequence"] == r["durable_sequence"] == 1
            and r["receipt"]["kind"] == 2
        )
    signed = json.loads(
        (directory / "chronicle-signed-body-throughput.json").read_text()
    )[1:]
    for r in signed:
        assert r["transactions"] == r["known_signatures"]
        assert (
            abs(
                r["commit_inclusive_tps"]
                - r["transactions"] * 1e6 / r["commit_inclusive_us"]
            )
            < 1
        )
    mixed = json.loads((directory / "chronicle-mixed-peers.json").read_text())
    reference = mixed["sv_reference"]
    raw = bytes.fromhex(mixed["candidate_block_hex"])
    assert txid(raw[:80]) == reference["active_hash"] == mixed["solved_hash"]
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == reference["body_sha256"] and all(
        r["body_sha256"] == digest for r in mixed["independent_receipts"]
    )
    assert (
        len(reference["successors"]) == 2
        and reference["successors"][0] == mixed["successor"]
    )
    assert reference["successors"][-1] == reference["settlement_tip"]
    for name, count in (("workload", 8), ("sustained", 128)):
        data = json.loads((directory / f"chronicle-native-{name}.json").read_text())
        timed = data["rows"]
        assert len(timed) == count and [r["height"] for r in timed] == list(
            range(107, 107 + count)
        )
        assert all(
            r["transactions"] == r["known_signature_checks"] == 4096 for r in timed
        )
        assert all(
            0 < r["reference_return_ns"] <= r["durable_observed_ns"] for r in timed
        )
        assert (
            data["final_reference_tip"] == timed[-1]["hash"]
            and len({r["hash"] for r in timed}) == count
        )
        assert sum(r["transactions"] for r in timed) == data["total_transactions"]
        assert (
            abs(
                data["transactions_per_second"]
                - data["total_transactions"] * 1e9 / data["total_wall_ns"]
            )
            < 0.001
        )
    recovery = json.loads((directory / "chronicle-capacity-recovery.json").read_text())
    for phase in ("before", "exhausted", "restored"):
        row = recovery[phase]
        assert (
            row["height"] == 106
            and row["tip_hash"] == recovery["before"]["tip_hash"]
            and row["witnessed_outputs"] == 1
        )
    assert not recovery["exhausted"]["observation_window_ready"]
    assert (
        recovery["repaired"]["tip_hash"] == recovery["reference_tip"]
        and recovery["repaired"]["height"] == 108
    )
    assert (
        recovery["repaired"]["witnessed_outputs"] == 3
        and recovery["repaired"]["observation_window_ready"]
    )
    large = json.loads((directory / "chronicle-large-body.json").read_text())
    assert (
        txid(bytes.fromhex(large["header_hex"]))
        == large["hash"]
        == large["restored_tip"]
    )
    assert (
        large["wire_bytes"] == large["mapped_bytes"] == large["restored_mapped_bytes"]
        and large["wire_bytes"] > 12 << 20
    )
    assert (
        large["known_signature_checks"] == 1
        and large["reference_tip"] == large["successor"]
    )
    assert (
        large["rollback"]["restored_tip"] == large["successor"]
        and large["rollback"]["reference_tip"] == large["rollback"]["next_active_tip"]
    )
    print(
        f"Verified {len(rows)} differential cases, both solved DAR bodies, reorg dispositions and both directive orderings."
    )


if __name__ == "__main__":
    verify(ROOT / "docs/evidence")
