"""Obtain a signed spend solely from a stock Teranode's completed, unmined subtree."""

import argparse
import contextlib
import hashlib
import json
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path

from native_cohort import stratum_job
from peer_loss import nonce
from prove import DisposableWallet, block, dsha, request, rpc, txid, wait
from run_cohort import session_lock
from scary_dar import ROOT, get_port, response


def run(binary, directory):
    config = json.loads(
        subprocess.check_output(
            [
                "docker",
                "compose",
                "-f",
                str(ROOT / "regtest/compose.yaml"),
                "config",
                "--format",
                "json",
            ]
        )
    )["services"]["tn1"]
    r, assets, native, api, worker, local_assets = [get_port() for _ in range(6)]
    full = (r, assets)
    name = "scary-subtree-" + uuid.uuid4().hex
    pub = f"http://127.0.0.1:{assets}/api/v1"
    env = dict(
        config["environment"],
        asset_httpPublicAddress=pub,
        p2p_static_peers="",
        initial_merkle_items_per_subtree="2",
        minimum_merkle_items_per_subtree="2",
        blockassembly_useDynamicSubtreeSize="false",
    )
    args = ["docker", "run", "--detach", "--name", name]
    for port, internal in [(r, 9292), (assets, 8090), (native, 9905)]:
        args.extend(["-p", f"127.0.0.1:{port}:{internal}"])
    for key, value in env.items():
        args.extend(["-e", f"{key}={value}"])
    subprocess.run(
        [*args, config["image"], *config["command"]],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    with contextlib.ExitStack() as cleanup:
        cleanup.callback(
            subprocess.run,
            ["docker", "rm", "--force", "--volumes", name],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        wait(
            "fresh stock Teranode",
            lambda: rpc(full, "getblockchaininfo")["chain"] == "regtest",
        )
        wallet = DisposableWallet(directory / "wallet.pem")
        first = rpc(full, "generatetoaddress", 1, wallet.address)[0]
        _, txs, outs = wait("wallet coinbase", lambda: block(full, first))
        funding_coin, value = txid(txs[0]), outs[0][0][0]
        assert outs[0][0][1] == wallet.script
        mature = rpc(full, "generate", 101)[-1]

        def stop(proc):
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=30)

        with (
            (directory / "node.log").open("w") as log,
            (directory / "diagnostics.log").open("w") as diagnostics,
        ):
            proc = subprocess.Popen(
                [
                    str(binary),
                    "--network",
                    "regtest",
                    "--peer",
                    f"/ip4/127.0.0.1/tcp/{native}/p2p/{env['p2p_peer_id']}",
                    "--data-dir",
                    str(directory / "node"),
                    "--observe-bodies",
                    "--api-bind",
                    f"127.0.0.1:{api}",
                    "--stratum-bind",
                    f"127.0.0.1:{worker}",
                    "--asset-bind",
                    f"127.0.0.1:{local_assets}",
                    "--asset-url",
                    f"http://host.docker.internal:{local_assets}/api/v1",
                    "--payout-script",
                    "51",
                ],
                stdout=log,
                stderr=diagnostics,
            )
        cleanup.callback(stop, proc)

        def status():
            if proc.poll() is not None:
                raise ValueError(
                    "entrant exited: "
                    + (directory / "diagnostics.log").read_text()[-400:]
                )
            return response(f"http://127.0.0.1:{api}/status")

        wait(
            "fresh entrant selects native contemporary tip",
            lambda: status()["mining"]["tip_hash"] == mature,
        )
        binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
        assert status()["release"]["executable_sha256"] == binary_sha256
        funding = wallet.spend(funding_coin, value, 1000)
        funding_id = txid(funding)
        assert rpc(full, "sendrawtransaction", funding.hex()) == funding_id
        wait(
            "reference assembled the funding payment",
            lambda: rpc(full, "getminingcandidate")["num_tx"] >= 1,
        )
        creating = rpc(full, "generate", 1)[0]
        assert (
            funding in wait("reference mined funding", lambda: block(full, creating))[1]
        )
        wait(
            "creating block observed",
            lambda: status()["mining"]["tip_hash"] == creating,
        )
        parent = rpc(full, "generate", 1)[0]
        wait(
            "creating block has observed successor",
            lambda: status()["mining"]["tip_hash"] == parent,
        )
        payment = wallet.spend(funding_id, value - 1000, 1000)
        payment_id = txid(payment)
        before = status()["mining"]
        assert (
            before["observation_window_ready"] and before["pending_transactions"] == 0
        )
        assert rpc(full, "sendrawtransaction", payment.hex()) == payment_id

        def received():
            m = status()["mining"]
            return m if m["pending_transactions"] == 1 else None

        after = wait(
            "unmined signed payment in verified remote subtree", received, seconds=45
        )
        assert after["tip_hash"] == parent and rpc(full, "getbestblockhash") == parent
        b, a = [m["peer_transaction_intake"] for m in [before, after]]
        assert (
            a["teranode_subtree"]["admitted"] == b["teranode_subtree"]["admitted"] + 1
        )
        assert a["sv"] == b["sv"] and a["sv"]["admitted"] == 0
        roots = {payment_id, txid(bytes([255]) * 32 + dsha(payment))}

        def event_for(kind, hashes):
            lines = (directory / "node.log").read_text().splitlines(keepends=True)
            for line in reversed(lines):
                if line.endswith("\n") and line.startswith("{"):
                    row = json.loads(line)
                    if row.get("event") == kind and row.get("hash") in hashes:
                        return row
            return None

        notice = wait(
            "signed payment subtree notice", lambda: event_for("subtree_notice", roots)
        )
        root = notice["hash"]
        subtree_data = request(f"{pub}/subtree_data/{root}")
        completion = None
        if "subtree_processed" in status()["peer"]:
            event = wait(
                "completed signed subtree on the same selected parent",
                lambda: event_for("subtree_processed", {root}),
            )
            peer = status()["peer"]
            assert event["transactions"] == 1 and event["bytes"] == len(subtree_data)
            assert (
                peer["subtree_processed"] >= 1
                and peer["subtree_processed_transactions"] >= 1
            )
            completion = {
                "event": event,
                "workers": peer["subtree_fetch_workers"],
                "waiting_capacity": peer["subtree_queue_capacity"],
                "successful_root_completions": peer["subtree_processed"],
                "failed_root_completions": peer["subtree_failed"],
            }
        with stratum_job(parent, payment, worker, api, "native-subtree-payment") as job:
            solution = nonce(job)
            header = job.header(solution)
            solved = txid(header)
            assert job.submit_native(solution)
        wait(
            "Teranode selects the entrant's published block",
            lambda: rpc(full, "getbestblockhash") == solved,
        )
        wire, txs, _ = block(full, solved)
        assert txs[1:] == [payment]
        successor = rpc(full, "generate", 1)[0]
        wait(
            "native independent successor observed",
            lambda: status()["mining"]["tip_hash"] == successor,
        )
        next_wire = block(full, successor)[0]
        assert next_wire[4:36][::-1].hex() == solved
        return {
            "network": "regtest",
            "reference": config["image"],
            "binary_sha256": binary_sha256,
            "ingress": "Stock Teranode received sendrawtransaction and announced the resulting subtree over native P2P.",
            "subtree_leaf_capacity": 2,
            "creating_block": creating,
            "creating_block_wire": block(full, creating)[0].hex(),
            "parent_header": block(full, parent)[0][:80].hex(),
            "subtree_hash": root,
            **({"subtree_completion": completion} if completion else {}),
            "subtree_data_hex": subtree_data.hex(),
            "funding_hex": funding.hex(),
            "parent": parent,
            "payment_hex": payment.hex(),
            "payment_txid": payment_id,
            "peer_intake_before": b,
            "peer_intake_after": a,
            "stratum_header": header.hex(),
            "solved_block": wire.hex(),
            "block_hash": solved,
            "successor": successor,
            "successor_header": next_wire[:80].hex(),
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/scary-node")
    args = p.parse_args()
    with session_lock(), tempfile.TemporaryDirectory() as d:
        result = run(args.binary, Path(d))
    args.receipt.write_text(json.dumps(result, indent=2) + "\n")
