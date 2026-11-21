"""Check the shipped result tables, solved bytes and independent peer outcomes."""

import gzip
import hashlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "regtest"))
from prove import decode_block, dsha, txid
from verify_receipt import check_header, single_input
from cohort_estimate import analyze
from relay_comparison import paired_report
from verify_native_workers import verify as verify_workers, verify_retained
from shadow import audit
from verify_dar_history import verify as verify_dar_history
from verify_stream import verify as verify_stream
from verify_peer_intake import verify as verify_peer_intake
from verify_retained_delivery import verify as verify_retained_delivery


def verify_block_context(record):
    history = [bytes.fromhex(h) for h in record["headers_before_cases"]]
    assert len(history) == 11
    for i, header in enumerate(history):
        check_header(header, txid(header), txid(history[i - 1]) if i else None)
    assert [r["height"] for r in record["rows"]] == [575, 576, 577]
    for row in record["rows"]:
        body = bytes.fromhex(row["block_hex"])
        h, block_hash = row["height"], row["sv_active_hash"]
        assert row["parent_hash"] == txid(history[-1])
        check_header(body[:80], block_hash, row["parent_hash"])
        assert row["scary_active_hash"] == row["observation"]["hash"] == block_hash
        assert (
            int.from_bytes(body[:4], "little", signed=True)
            == row["header_version"]
            == 1
        )
        mtp = sorted(int.from_bytes(b[68:72], "little") for b in history[-11:])[5]
        block_time = int.from_bytes(body[68:72], "little")
        assert mtp == row["parent_median_time"] and block_time == row["header_time"]
        _, txs, _ = decode_block(body, block_hash)
        assert len(txs) == (2 if h in (575, 576) else 1)
        root = dsha(txs[0]) if len(txs) == 1 else dsha(dsha(txs[0]) + dsha(txs[1]))
        assert body[36:68] == root
        source, vout, coinbase_script = single_input(txs[0])
        assert (source, vout) == ("00" * 32, 0xFFFFFFFF)
        if h == 577:
            assert coinbase_script == bytes.fromhex("6a00")
        assert row["coinbase_sequence"] == 0xFFFFFFFE
        for raw in txs:
            # These single-input fixtures all use short scripts and compact counts.
            script_length = raw[41]
            assert script_length < 253
            assert raw[42 + script_length : 46 + script_length] == bytes.fromhex(
                "feffffff"
            )
            assert (
                int.from_bytes(raw[-4:], "little")
                == (block_time if h < 576 else mtp) - 1
            )
        assert int.from_bytes(txs[0][-4:], "little") == row["coinbase_lock_time"]
        obs = row["observation"]
        assert obs["coinbase_finality_checked"] and obs["coinbase_reward_checked"]
        if len(txs) == 2:
            assert single_input(txs[1])[0] == record["funding_txid"]
            assert txid(txs[1]) == row["payment_txid"]
            assert int.from_bytes(txs[1][-4:], "little") == row["payment_lock_time"]
            assert obs["transactions_determinate"] == obs["script_checks_passed"] == 1
            assert (row["payment_lock_time"] >= mtp) == (h == 575)
        history.append(body[:80])


def verify_acceptance(rows, dar, native, race):
    assert len(rows) == 62
    for row in rows:
        for mode in ("policy", "consensus"):
            assert (
                len({row["observations"][name][mode] for name in ("rust", "go", "sv")})
                == 1
            )
        if "expected_consensus" in row:
            assert row["observations"]["sv"]["consensus"] == row["expected_consensus"]
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
    first, second = race
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


def verify(directory):
    with gzip.open(directory / "chronicle-receiver-dar-history.json.gz", "rt") as file:
        dar_history = verify_dar_history(json.load(file))
    replay = json.loads((directory / "chronicle-receiver-dar-replay.json").read_text())
    fresh_reference = replay.pop("independent_replay")
    assert replay == dar_history
    assert fresh_reference == {
        "fresh_sv_active_tip": dar_history["sv_final_active_tip"],
        "confirmed_detached_blocks": dar_history["detached_blocks"],
        "confirmed_active_blocks": dar_history["active_blocks"],
    }
    linux, second, mac, restart = (
        json.loads((directory / filename).read_text())
        for filename in (
            "chronicle-qualification-linux-release.json",
            "chronicle-second-linux-build.json",
            "chronicle-qualification-macos-release.json",
            "chronicle-shadow-native-intake-restart.json",
        )
    )
    assert linux["image_id"] != second["image_id"]
    assert linux["source_commit"] != second["source_commit"]
    assert linux["release"] == second["release"]
    assert linux["interpreter"] == second["interpreter"]
    assert (
        linux["cargo_lock_sha256"]
        == second["cargo_lock_sha256"]
        == mac["cargo_lock_sha256"]
    )
    assert second["source_commit"] == mac["source_commit"] == restart["source_commit"]
    assert (
        mac["executable_sha256"]
        == mac["operator_probe"]["release_sha256"]
        == restart["release_sha256"]
    )
    for key in ("bdk_commit", "sv_node_commit"):
        assert mac["native_build"][key] == linux["interpreter"][key]
    assert restart["passive_mainnet"] and restart["receiver"]["synced"]
    before, restored, after = (
        restart[key] for key in ("before", "restored", "reconfirmed")
    )
    for key in (
        "height",
        "tip_hash",
        "coverage_start",
        "coverage_end",
        "headers",
        "witnessed_outputs",
        "window_chainwork",
        "durable_directive_sequence",
    ):
        assert before[key] == restored[key] == after[key]
    assert restart["recovered_start_hash"] == after["tip_hash"]
    assert after["coverage_end"] - after["coverage_start"] + 1 == after["headers"]
    assert (
        not restored["tip_confirmed_since_disconnect"]
        and after["tip_confirmed_since_disconnect"]
    )
    assert after["connected_data_peers"] > 0 and after["alert_ready"]
    assert after["durable_directive_sequence"] == restart["receiver"]["sequence"]
    assert restart["receiver"]["unprocessed_alerts"] == 0

    verify_retained_delivery(
        json.loads((directory / "chronicle-retained-delivery.json").read_text())
    )
    native = json.loads((directory / "chronicle-native-successors.json").read_text())
    assert len(native["rows"]) == 12
    parent = native["starting_parent"]
    for i, row in enumerate(native["rows"]):
        wire = bytes.fromhex(row["wire_hex"])
        assert (i % 2 == 0) == (row["issuer"] == "sv")
        check_header(wire[:80], row["hash"], parent)
        decode_block(wire, row["hash"])
        assert hashlib.sha256(wire).hexdigest() == row["wire_sha256"]
        assert row["receiver_hashes"] == dict.fromkeys(
            ("sv", "tn1", "tn2", "entrant"), row["hash"]
        )
        parent = row["hash"]

    large_stream = verify_stream(directory / "chronicle-mixed-sustained.json.gz")
    assert (
        large_stream["candidate_transactions"],
        large_stream["active_wire_matches"],
        large_stream["background_payments"],
    ) == (49176, 72, 96)
    lease = json.loads((directory / "chronicle-mining-lease.json").read_text())
    assert lease["first_subscription"] == lease["legacy_watermark"] + 1
    assert lease["last_primary_subscription"] == lease["first_subscription"] + 1024
    assert lease["first_restored_subscription"] == lease["first_subscription"] + 2048
    assert (
        lease["shutdown"] == "SIGKILL"
        and lease["parallel_distinct_identity_subscribed"]
    )

    verify_block_context(
        json.loads((directory / "chronicle-block-context.json").read_text())
    )
    items = json.loads((directory / "chronicle-alert-items.json").read_text())
    first, second = items["freeze_results"]
    expected = {
        "notProcessed": [
            {
                "txOut": {"txId": items["funding_txid"], "vout": 0},
                "reason": "already in consensus",
            }
        ]
    }
    for row, seq, result in [(first, 1, {"notProcessed": []}), (second, 2, expected)]:
        assert row["sequence"] == row["receipt"]["sequence"] == seq
        assert row["scary"] == row["sv"] == row["receipt"]["rpc_result"] == result
        assert row["receipt"]["active_height"] == items["height"]
    rejected = items["unapplied"]
    assert rejected["failure"]["sequence"] == rejected["last_verified"] == 3
    assert rejected["last_applied"] == 2 and not rejected["ready"]
    assert rejected["after_restart"] == {
        "last_verified": 3,
        "last_applied": 2,
        "ready": False,
    }
    assert rejected["sv"] == rejected["scary"] == rejected["failure"]["rpc_result"]
    assert (
        rejected["sv"]["notProcessed"][0]["reason"]
        == "confiscated TXO is not consensus frozen"
    )
    rows = json.loads((directory / "chronicle-opcodes.json").read_text())["rows"]
    with gzip.open(directory / "chronicle-signatures.json.gz", "rt") as file:
        rows += json.load(file)["rows"]
    verify_acceptance(
        rows,
        json.loads((directory / "scary-dar-lifecycle.json").read_text()),
        json.loads((directory / "chronicle-receiver-dar.json").read_text()),
        json.loads((directory / "chronicle-directive-race.json").read_text()),
    )
    with gzip.open(directory / "chronicle-release-repeat.json.gz", "rt") as file:
        repeat = json.load(file)
    for key in ("node_release_sha256", "rust_oracle_sha256", "go_oracle_sha256"):
        assert len(bytes.fromhex(repeat[key])) == 32
    verify_acceptance(
        repeat["chronicle"]["rows"] + repeat["signatures"]["rows"],
        repeat["signed-receiver"],
        repeat["signed-receiver"],
        repeat["directive-race"],
    )
    for name in (
        "chronicle-sv-transaction-intake.json",
        "chronicle-teranode-subtree-intake.json",
    ):
        verify_peer_intake(json.loads((directory / name).read_text()))
    bounded = json.loads((directory / "chronicle-candidate-capacity.json").read_text())
    body = bytes.fromhex(bounded["block_hex"])
    check_header(body[:80], bounded["block"], bounded["parent"])
    _, transactions, _ = decode_block(body, bounded["block"])
    assert [txid(tx) for tx in transactions[1:]] == bounded["accepted_txids"]
    assert bounded["wire_bytes"] == len(body) <= bounded["body_limit"] == 1024
    assert (
        bounded["declined"]
        and len(bounded["accepted_txids"]) + len(bounded["declined"]) == 8
    )
    assert all(
        len(body) + row["bytes"] > bounded["body_limit"] for row in bounded["declined"]
    )
    assert bounded["receiver_wire_sha256"] == [hashlib.sha256(body).hexdigest()] * 2
    successor = bytes.fromhex(bounded["successor_header"])
    check_header(successor, txid(successor), bounded["block"])
    assert bounded["p2sh_mining_payout_refused"]
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
    assert fault["process_exit"] == 1 and all(
        fault["before"][k] == fault["restored"][k]
        for k in ("height", "tip_hash", "witnessed_outputs")
    )
    assert (
        fault["concurrent_writer_refused"] and fault["incomplete_temporary_bytes"] > 0
    )
    assert fault["repaired"]["tip_hash"] == fault["reference_tip"]
    assert fault["repaired"]["height"] == fault["before"]["height"] + 2
    crash = fault["interrupted_write"]
    assert (
        crash["signal"] == "SIGKILL"
        and crash["bytes_read_from_inflight_checkpoint"] == 4096
    )
    assert (
        crash["repaired"]["tip_hash"] == crash["reference_tip"]
        and crash["replayed_known_signatures"] == 1
    )
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
    shadow = json.loads((directory / "chronicle-shadow-progress.json").read_text())
    journal = io.StringIO("\n".join(json.dumps(r) for r in shadow["records"]))
    assert audit(journal) == shadow["audit"]
    assert shadow["audit"]["sv_only_failovers"] >= 1
    restricted, received, restored = shadow["failover"]
    assert (restricted["event"], received["event"], restored["event"]) == (
        "restrict_body_transport",
        "sv_body_received",
        "restore_all_body_transports",
    )
    assert received["contiguous_failover"] and received["restored"]
    assert any(
        r.get("event") == "tip"
        and r["hash"] == received["hash"]
        and r["previous"] == restricted["hash"]
        and r["height"] == restricted["height"] + 1
        for r in shadow["records"]
    )
    assert (
        verify_stream(directory / "chronicle-mixed-stream.json.gz")[
            "active_wire_matches"
        ]
        == 24
    )
    live = json.loads((directory / "chronicle-mainnet-alert-sync.json").read_text())
    assert (
        live["genesis_sha256"]
        == hashlib.sha256(
            bytes.fromhex(
                (ROOT / "qualification/alert-genesis.hex").read_text().strip()
            )
        ).hexdigest()
    )
    assert (
        live["signed_sequence"]
        == live["verified_sequence"]
        == live["durable_sequence"]
        == 1
    )
    assert (
        live["genesis_verified"]
        and live["receiver_active_peers"] >= 1
        and live["unprocessed_alerts"] == 0
    )
    assert analyze(directory / "chronicle-native-pilot", "pilot") == json.loads(
        (directory / "chronicle-native-sizing.json").read_text()
    )
    assert paired_report(directory / "chronicle-relay-comparison") == json.loads(
        (directory / "chronicle-relay-comparison.json").read_text()
    )
    for name in (
        "chronicle-workers-32.json.gz",
        "chronicle-workers-32-revised.json.gz",
    ):
        with gzip.open(directory / name, "rt") as stream:
            summary = verify_workers(json.load(stream))
        assert summary["workers_per_parent"] == 32 and summary["parents"] == 12
    with gzip.open(directory / "chronicle-retained-job.json.gz", "rt") as stream:
        assert verify_retained(json.load(stream))["same_parent_replacements"] == 16
    loss = json.loads((directory / "chronicle-peer-loss.json").read_text())
    assert (
        loss["lost"]["tip_hash"] == loss["initial_parent"]
        and loss["lost"]["connected_data_peers"] == 0
    )
    assert (
        not loss["lost"]["observation_window_ready"]
        and loss["lost"]["solved_blocks"] == 0
    )
    assert (
        loss["repaired"]["tip_hash"] == loss["advanced_while_disconnected"]
        and loss["repaired"]["observation_window_ready"]
    )
    assert loss["new_solution"] == loss["full_active_tip"]
    print(
        f"Verified {len(rows)} differential cases, both solved DAR bodies, reorg dispositions and both directive orderings."
    )


if __name__ == "__main__":
    verify(ROOT / "docs/evidence")
