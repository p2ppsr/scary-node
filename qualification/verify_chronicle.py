"""Check the shipped result tables, solved bytes and independent peer outcomes."""

import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "regtest"))
from prove import decode_block, dsha, txid
from verify_receipt import check_header, single_input


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
    native = json.loads((directory / "chronicle-receiver-dar.json").read_text())
    receiver = native["receiver"]
    assert (
        receiver["active_peers"] > 0
        and receiver["unprocessed_alerts"] == 0
        and receiver["sequence"] == receiver["last_peer_sync"]["sequence"]
    )
    assert receiver["sequence"] == len(native["signed_records"]) == 10
    assert [r["sequence_number"] for r in native["signed_records"]] == list(
        range(1, 11)
    )
    lifecycle = native["directive_lifecycle"]
    assert [r["kind"] for r in lifecycle] == [1, 2, 3, 5, 6, 7, 8, 1]
    for r in lifecycle:
        state = r["directive_state"]
        assert (
            state["ready"]
            and state["last_applied"] == state["last_verified"] == r["sequence"]
        )
    assert [
        r["reference_tcp_connections"] for r in lifecycle if r["kind"] in (5, 6)
    ] == [0, 1]
    released = native["ordinary_freeze_unfreeze"]
    assert (
        "frozen" in released["entrant_rejection"]
        and "frozen" in released["sv_rejection"]
    )
    assert len(bytes.fromhex(released["inclusion"])) == 32
    for raw, block in (
        ("confiscation_block_hex", "confiscation_block"),
        ("recipient_block_hex", "confirmed_hash"),
    ):
        assert txid(bytes.fromhex(native[raw])[:80]) == native[block]
    assert native["scary_active_tip"] == native["remined_spend_hash"]
    assert native["reorg_solutions"][-1]["chain_state"] == "stale_at_depth"
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
    with gzip.open(directory / "chronicle-mixed-workload.json.gz", "rt") as file:
        workload = json.load(file)["mixed_workload"]
    count = workload["transactions"]
    assert count == 4096
    fund = bytes.fromhex(workload["funding_block_hex"])
    check_header(fund[:80], workload["funding_block"])
    _, funding_txs, funding_outputs = decode_block(fund, workload["funding_block"])
    index = next(i for i, t in enumerate(funding_txs) if txid(t) == workload["funding"])
    values = funding_outputs[index]
    assert len(values) == count
    check_header(
        bytes.fromhex(workload["parent_header_hex"]),
        workload["parent"],
        workload["funding_block"],
    )
    body = bytes.fromhex(workload["body_hex"])
    solved = workload["solved_hash"]
    check_header(body[:80], solved, workload["parent"])
    _, txs, outputs = decode_block(body, solved)
    assert len(txs) == count + 1 and len(body) == workload["wire_bytes"]
    inputs = set()
    fees = 0
    for tx, produced in zip(txs[1:], outputs[1:]):
        h, n, _ = single_input(tx)
        assert h == workload["funding"] and n not in inputs and n < count
        inputs.add(n)
        fee = values[n][0] - sum(v for v, _ in produced)
        assert fee == 1000
        fees += fee
    assert fees == workload["fee_satoshis"]
    assert (
        sum(v for v, _ in outputs[0])
        == (5_000_000_000 >> (workload["height"] // 150)) + fees
    )
    digest = hashlib.sha256(body).hexdigest()
    receivers = workload["receivers"]
    assert len(receivers) == 3 and {r["receiver"] for r in receivers} == {
        "tn1",
        "tn2",
        "sv",
    }
    assert all(r["hash"] == solved and r["body_sha256"] == digest for r in receivers)
    previous = solved
    for encoded, expected in zip(
        workload["successor_headers_hex"],
        workload["settlement"]["successors"],
        strict=True,
    ):
        check_header(bytes.fromhex(encoded), expected, previous)
        previous = expected
    assert (
        len(workload["successor_headers_hex"]) == 2
        and previous == workload["settlement"]["settlement_tip"]
    )
    fault = json.loads((directory / "chronicle-storage-fault.json").read_text())
    assert fault["process_exit"] == 1 and fault["before"] == fault["restored"]
    assert (
        fault["concurrent_writer_refused"] and fault["incomplete_temporary_bytes"] > 0
    )
    assert fault["repaired"]["tip_hash"] == fault["reference_tip"]
    assert fault["repaired"]["height"] == fault["before"]["height"] + 2
    clock = json.loads((directory / "chronicle-clock-fault.json").read_text())
    assert clock["solution"] == clock["reference_tip"]
    assert [r["offset"] for r in clock["held_submissions"]] == ["+600s", "-600s"]
    for r in clock["held_submissions"]:
        st = r["status"]
        assert st["tip_hash"] == clock["parent"] and not st["observation_window_ready"]
        assert (
            abs(st["clock_step_seconds"] - int(r["offset"][:-1])) <= 1
            and st["solved_blocks"] == 0
        )
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
