"""Exercise a fork beyond the undo journal, with and without signed block invalidation."""

import argparse
import contextlib
import hashlib
import json
import tempfile
import time
from pathlib import Path

from native_cohort import funding_transactions, stratum_job
from peer_loss import nonce
from prove import dsha
from run_cohort import session_lock
from scary_dar import ROOT, LabFeed, empty_successor, entrant, get_port, response, until
from sv_reference import IMAGE, Reference


def case(binary, directory, name, expect_hold=False):
    signed = name == "signed"
    wide = name == "offline_fork"
    depth = 32 if wide else 12
    with (
        Reference().running(peer=True, fast_rpc=True) as full,
        contextlib.ExitStack() as life,
    ):
        rpc = full.rpc
        rpc("generate", 105)
        feed = LabFeed(full, directory)
        life.callback(feed.server.shutdown)
        port, worker = get_port(), get_port()

        def session():
            return entrant(
                binary,
                full,
                feed,
                directory,
                port,
                worker,
                extra_args=("--min-observed-blocks", "2"),
            )

        def snap():
            m = response(f"http://127.0.0.1:{port}/status")["mining"]
            return {
                k: m[k]
                for k in [
                    "height",
                    "tip_hash",
                    "connected_data_peers",
                    "buffered_bodies",
                    "recoverable_blocks",
                    "coverage_start",
                    "headers",
                    "witnessed_outputs",
                    "observation_window_ready",
                    "job_preconditions_met",
                    "pending_transactions",
                    "readiness_reasons",
                    "durable_directive_sequence",
                ]
            }

        def tip(h):
            until(lambda: snap()["tip_hash"] == h, "entrant observes " + h)

        with session():
            # The issuer's mature funding predates entry. The entrant witnesses
            # the funding output itself, then the common predecessor to each fork.
            funding, (old_payment,) = funding_transactions(full, 1)
            rpc("sendrawtransaction", funding)
            creation = rpc("generate", 1)[0]
            tip(creation)
            anchor = rpc("generate", 1)[0]
            tip(anchor)
            old = []
            for _ in range(depth):
                h = rpc("generate", 1)[0]
                tip(h)
                old.append(h)
            rpc("sendrawtransaction", old_payment.hex())
            until(
                lambda: snap()["pending_transactions"] == 1, "old branch's peer payment"
            )
            before = snap()
            funding_id = dsha(bytes.fromhex(funding))[::-1].hex()

            def original_coin():
                return response(f"http://127.0.0.1:{port}/outpoints/{funding_id}/0")[
                    "known"
                ]

            old_known = original_coin()
            assert old_known and before["recoverable_blocks"] < len(old)
            pubkeys = list(feed.keys)
            sql_receipt = None
            signed_frame = None
            if signed:
                reason = b"Isolated regtest active branch invalidation"
                seq = feed.add(
                    7, bytes.fromhex(old[0])[::-1] + bytes([len(reason)]) + reason
                )
                until(
                    lambda: snap()["durable_directive_sequence"] == seq,
                    "durable signed invalidation",
                )
                assert not snap()["job_preconditions_met"]
                signed_frame = feed.records[-1]["raw"]
                signed_file = directory / "state/regtest/alerts" / f"{seq:010}.signed"
                assert signed_file.read_bytes().hex() == signed_frame
                action_receipt = signed_file.with_suffix(".receipt")
                sql_receipt = json.loads(action_receipt.read_text())
                held = snap()
            else:
                held = None
        with session():
            restarted = snap()
            assert restarted["tip_hash"] == before["tip_hash"]
            if signed:
                assert (
                    not restarted["observation_window_ready"]
                    and restarted["durable_directive_sequence"] == 1
                )
            # The independent stock node remains on the old branch until this
            # point. Its ordinary announcements exercise the policy boundary.
            unknown_offers = []
            unknown_hold = None
            if signed:
                assert signed_file.read_bytes().hex() == signed_frame
                assert json.loads(action_receipt.read_text()) == sql_receipt
                # The reference has yet to apply the directive. It advances
                # privately while the entrant is disconnected. Reconnection
                # exposes high, linked tips whose ancestry was never delivered.
                rpc("setnetworkactive", False)
                until(
                    lambda: snap()["connected_data_peers"] == 0,
                    "full peer disconnected",
                )
                for _ in range(27):
                    old.append(empty_successor(full))
                rpc("setnetworkactive", True)
                until(
                    lambda: snap()["connected_data_peers"] > 0,
                    "ordinary peer reconnected",
                )
                for offered in range(1, 3):
                    unknown_offers.append(empty_successor(full))
                    if not expect_hold:
                        until(
                            lambda n=offered: snap()["buffered_bodies"] >= n,
                            "unlinked full peer announcement received",
                        )
                old.extend(unknown_offers)
                if expect_hold:
                    time.sleep(1)
                unknown_hold = snap()
                assert (
                    unknown_hold["tip_hash"] == before["tip_hash"]
                    and not unknown_hold["observation_window_ready"]
                )
            offline = None
            if wide:
                rpc("setnetworkactive", False)
                until(
                    lambda: snap()["connected_data_peers"] == 0,
                    "offline before stock fork",
                )
                offline = snap()
                assert not offline["job_preconditions_met"]
            rpc("invalidateblock", old[0])
            assert rpc("getbestblockhash") == anchor
            # Stock SV protects wallet RPCs after a substantial fork. The lab
            # operator acknowledges exactly the displaced branch for that alarm.
            assert rpc("ignoresafemodeforblock", old[0]) is None
            alternate = []
            equal = None
            start_log = (directory / "node.log").stat().st_size
            for i in range(depth + 2):
                h = empty_successor(full)
                alternate.append(h)
                time.sleep(0.08)
                if i == depth - 1 and not signed and not wide:

                    def handled():
                        with (directory / "node.log").open("rb") as log:
                            log.seek(start_log)
                            return any(
                                line.startswith(b"{")
                                and (r := json.loads(line)).get("event")
                                == "body_observed"
                                and r.get("height") == before["height"]
                                for line in log
                                if line.endswith(b"\n")
                            )

                    until(handled, "equal-work alternative received")
                    equal = snap()
                    assert equal["tip_hash"] == before["tip_hash"]
            if wide:
                assert snap()["tip_hash"] == before["tip_hash"]
                rpc("setnetworkactive", True)
            if expect_hold:
                time.sleep(2)
                end = snap()
                assert (
                    end["tip_hash"] == before["tip_hash"]
                    and not end["job_preconditions_met"]
                ), end
                return {
                    "case": name,
                    "before": before,
                    "held": held,
                    "restart": restarted,
                    "unknown_offers": unknown_offers,
                    "held_with_unlinked_tips": unknown_hold,
                    "stock_tip": h,
                    "withheld": end,
                }
            tip(h)
            recovered = snap()
            old_still_known = original_coin()
            assert (
                recovered["coverage_start"] == before["height"] - depth + 1
                and recovered["headers"] == len(alternate)
                and not old_still_known
                and recovered["observation_window_ready"]
                and recovered["pending_transactions"] == 0
            )
            # New funds created after the re-entry boundary can pay a worker.
            newly_funded, (payment,) = funding_transactions(full, 1)
            rpc("sendrawtransaction", newly_funded)
            funding_block = rpc("generate", 1)[0]
            tip(funding_block)
            parent = rpc("generate", 1)[0]
            tip(parent)
            payment_id = rpc("sendrawtransaction", payment.hex())
            until(
                lambda: snap()["pending_transactions"] > 0,
                "payment received after recovery",
            )
            with stratum_job(parent, payment, worker, port, "after-recovery") as work:
                solution = nonce(work)
                solved = dsha(work.header(solution))[::-1].hex()
                assert work.submit_native(solution)
            until(
                lambda: rpc("getbestblockhash") == solved,
                "stock reference selected new-boundary block",
            )
            successor = rpc("generate", 1)[0]
            tip(successor)
            result = {
                "case": name,
                "source": IMAGE,
                "anchor": anchor,
                "anchor_wire": rpc("getblock", anchor, 0),
                "old_branch": [rpc("getblock", x, 0) for x in old],
                "alternative_branch": [rpc("getblock", x, 0) for x in alternate],
                "signed_frame": signed_frame,
                "lab_public_keys": pubkeys if signed else [],
                "application_receipt": sql_receipt,
                "unknown_offers": unknown_offers,
                "held_with_unlinked_tips": unknown_hold,
                "equal_work": equal,
                "offline_before_fork": offline,
                "old_funding_txid": funding_id,
                "old_funding_wire": rpc("getblock", creation, 0),
                "old_funding_observed": old_known,
                "old_funding_after_reset": old_still_known,
                "before": before,
                "held": held,
                "restart": restarted,
                "recovered": recovered,
                "funding_wire": rpc("getblock", funding_block, 0),
                "parent_wire": rpc("getblock", parent, 0),
                "payment_txid": payment_id,
                "solved_wire": rpc("getblock", solved, 0),
                "successor_wire": rpc("getblock", successor, 0),
                "final": snap(),
            }
        with session():
            until(
                lambda: (
                    snap()["tip_hash"] == successor
                    and snap()["observation_window_ready"]
                ),
                "restored fresh branch and peer confirmation",
            )
            result["restored_after_reentry"] = snap()
            assert result["restored_after_reentry"][
                "durable_directive_sequence"
            ] == int(signed)
            assert not original_coin()
            if signed:
                assert signed_file.read_bytes().hex() == signed_frame
                assert json.loads(action_receipt.read_text()) == sql_receipt
        return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("output", type=Path)
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/scary-node")
    p.add_argument("--expect-hold", action="store_true")
    p.add_argument(
        "--case",
        choices=["signed", "greater_work", "offline_fork", "all"],
        default="all",
    )
    opts = p.parse_args()
    results = []
    names = (
        ["greater_work", "signed", "offline_fork"]
        if opts.case == "all"
        else [opts.case]
    )
    digest = hashlib.sha256(opts.binary.read_bytes()).hexdigest()
    partial = opts.output.with_suffix(opts.output.suffix + ".partial")
    for name in names:
        with session_lock(), tempfile.TemporaryDirectory() as d:
            results.append(case(opts.binary, Path(d), name, opts.expect_hold))
        partial.write_text(
            json.dumps({"binary_sha256": digest, "cases": results}, indent=2) + "\n"
        )
    partial.replace(opts.output)
