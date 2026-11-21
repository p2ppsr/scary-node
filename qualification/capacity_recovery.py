"""Cross a configured witnessed-output budget, restart and recover the missing parent over SV P2P."""

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from scary_dar import ROOT, get_port, response, until
from sv_reference import Reference, IMAGE


@contextlib.contextmanager
def observer(binary, peer, directory, port, budget):
    with (directory / "log").open("ab") as log:
        proc = subprocess.Popen(
            [
                str(binary),
                "--network",
                "regtest",
                "--sv-peer",
                peer,
                "--observe-bodies",
                "--api-bind",
                f"127.0.0.1:{port}",
                "--data-dir",
                str(directory / "state"),
            ],
            env={**os.environ, "SCARY_MAX_WITNESSED_OUTPUTS": str(budget)},
            stdout=log,
            stderr=log,
        )
        try:
            yield
        finally:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=15)


def run(binary, directory):
    with Reference().running(peer=True) as n:
        n.rpc("generate", 105)
        port = get_port()
        url = f"http://127.0.0.1:{port}/status"
        peer = n.docker("port", n.container, "18444/tcp")

        def view():
            return response(url)["mining"]

        def capture():
            return {
                k: view()[k]
                for k in (
                    "height",
                    "tip_hash",
                    "witnessed_outputs",
                    "observation_window_ready",
                    "readiness_reasons",
                    "capacity",
                )
            }

        with observer(binary, peer, directory, port, 1):
            until(
                lambda: response(url)["peer"]["connected_peer_ids_seen"],
                "first handshake",
            )
            first = n.rpc("generate", 1)[0]
            until(lambda: view()["tip_hash"] == first, "initial committed boundary")
            before = capture()
            missing = n.rpc("generate", 1)[0]
            until(
                lambda: (
                    response(url)["peer"]["coverage_gaps"] > 0
                    and not view()["observation_window_ready"]
                ),
                "visible resource exhaustion",
            )
            blocked = capture()
            assert (
                blocked["tip_hash"] == first
                and blocked["witnessed_outputs"] == before["witnessed_outputs"]
            )
        with observer(binary, peer, directory, port, 64):
            until(
                lambda: response(url)["peer"]["connected_peer_ids_seen"],
                "recovered handshake",
            )
            restored = next(
                json.loads(line)
                for line in reversed((directory / "log").read_text().splitlines())
                if line.startswith('{"')
                and json.loads(line).get("event") == "witness_restored"
            )
            restored.pop("event")
            assert restored["tip_hash"] == first
            next_block = n.rpc("generate", 1)[0]
            until(
                lambda: (
                    view()["tip_hash"] == next_block
                    and view()["observation_window_ready"]
                ),
                "missing ancestor and child",
            )
            repaired = capture()
            assert (
                repaired["witnessed_outputs"] == 3
                and n.rpc("getblockhash", 107) == missing
            )
        return {
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "reference": IMAGE,
            "before": before,
            "exhausted": blocked,
            "restored": restored,
            "repaired": repaired,
            "retrieved_parent": missing,
            "reference_tip": next_block,
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/scary-node")
    a = p.parse_args()
    with tempfile.TemporaryDirectory() as td:
        result = run(a.binary, Path(td))
    a.receipt.write_text(json.dumps(result, indent=2) + "\n")
