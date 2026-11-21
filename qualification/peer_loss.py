"""Disable the sole native peer while its Stratum job is open, then reconcile the newly mined parent."""

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import tempfile
from native_cohort import funding_transactions, stratum_job, REGTEST_TARGET, dsha
from scary_dar import ROOT, LabFeed, entrant, get_port, response, until
from sv_reference import IMAGE, Reference


def nonce(job):
    return next(
        i
        for i in range(1024)
        if int.from_bytes(dsha(job.header(i)), "little") <= REGTEST_TARGET
    )


def run(binary, directory):
    with (
        Reference().running(peer=True, fast_rpc=True) as full,
        contextlib.ExitStack() as life,
    ):
        full.rpc("generate", 105)
        feed = LabFeed(full, directory)
        life.callback(feed.server.shutdown)
        port, worker = get_port(), get_port()
        api = life.enter_context(entrant(binary, full, feed, directory, port, worker))

        def snapshot():
            m = response(api + "/status")["mining"]
            return {
                k: m[k]
                for k in (
                    "height",
                    "tip_hash",
                    "connected_data_peers",
                    "tip_confirmed_since_disconnect",
                    "observation_window_ready",
                    "readiness_reasons",
                    "solved_blocks",
                    "pending_transactions",
                )
            }

        funding, (payment,) = funding_transactions(full, 1)
        full.rpc("sendrawtransaction", funding)
        h = full.rpc("generate", 1)[0]
        until(lambda: snapshot()["tip_hash"] == h, "funding observation")
        parent = full.rpc("generate", 1)[0]
        until(lambda: snapshot()["tip_hash"] == parent, "contiguous parent")
        assert (
            response(api + "/transactions", payment)["txid"]
            == dsha(payment)[::-1].hex()
        )
        with stratum_job(parent, payment, worker, port, "before-loss") as old:
            old_nonce = nonce(old)
            assert full.rpc("setnetworkactive", False) is False
            until(
                lambda: snapshot()["connected_data_peers"] == 0,
                "actual native disconnect",
            )
            lost = snapshot()
            assert (
                not lost["observation_window_ready"]
                and not lost["tip_confirmed_since_disconnect"]
            )
            assert (
                "awaiting current tip from a connected data peer"
                in lost["readiness_reasons"]
            )
            try:
                old.submit_native(old_nonce)
            except (OSError, RuntimeError, AssertionError, ValueError):
                pass
            else:
                raise AssertionError("job on the disconnected parent was accepted")
            assert (
                snapshot()["solved_blocks"] == 0
                and full.rpc("getbestblockhash") == parent
            )
        advanced = full.rpc("generate", 1)[0]
        assert full.rpc("setnetworkactive", True) is True
        until(
            lambda: (
                snapshot()["tip_hash"] == advanced
                and snapshot()["observation_window_ready"]
            ),
            "header probe and active body recovery",
        )
        repaired = snapshot()
        with stratum_job(advanced, payment, worker, port, "after-loss") as job:
            n = nonce(job)
            solved = dsha(job.header(n))[::-1].hex()
            assert job.submit_native(n)
        until(
            lambda: full.rpc("getbestblockhash") == solved,
            "new parent solved and selected",
        )
        return {
            "reference": IMAGE,
            "release_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "initial_parent": parent,
            "lost": lost,
            "advanced_while_disconnected": advanced,
            "repaired": repaired,
            "new_solution": solved,
            "full_active_tip": full.rpc("getbestblockhash"),
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/scary-node")
    a = p.parse_args()
    with tempfile.TemporaryDirectory() as d:
        result = run(a.binary, Path(d))
    a.receipt.write_text(json.dumps(result, indent=2) + "\n")
