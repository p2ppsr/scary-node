"""Step only the entrant's wall clock on an isolated regtest and check an actual Stratum job."""

import argparse
import contextlib
import json
import uuid
from pathlib import Path

from native_cohort import (
    funding_transactions,
    stratum_job,
    REGTEST_TARGET,
    dsha,
    response,
    until,
)
from sv_reference import Reference, IMAGE


def measure(image):
    stock = Reference()
    docker = stock.docker
    name = "scary-clock-" + uuid.uuid4().hex
    bridge = name + "-net"
    with contextlib.ExitStack() as life:
        docker("network", "create", bridge)
        life.callback(docker, "network", "rm", bridge)
        life.enter_context(stock.running(peer=True, network=bridge, fast_rpc=True))
        stock.rpc("generate", 105)
        ip = docker(
            "inspect",
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            stock.container,
        )
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            bridge,
            "--tmpfs",
            "/state",
            "-p",
            "127.0.0.1::8080",
            "-p",
            "127.0.0.1::3333",
            "--entrypoint",
            "sleep",
            image,
            "infinity",
        )
        life.callback(docker, "rm", "-fv", name)
        api = int(docker("port", name, "8080/tcp").rsplit(":", 1)[1])
        stratum = int(docker("port", name, "3333/tcp").rsplit(":", 1)[1])
        base = f"http://127.0.0.1:{api}"

        def step(value):
            docker(
                "exec",
                name,
                "sh",
                "-c",
                'echo "$1" > /tmp/clock.next && mv /tmp/clock.next /tmp/clock',
                "step",
                value,
            )

        step("+0s")
        docker(
            "exec",
            "-d",
            "-e",
            "LD_PRELOAD=/usr/local/lib/qualification-clock.so",
            "-e",
            "FAKETIME_NO_CACHE=1",
            "-e",
            "FAKETIME_DONT_FAKE_MONOTONIC=1",
            "-e",
            "FAKETIME_TIMESTAMP_FILE=/tmp/clock",
            name,
            "scary-node",
            "--network",
            "regtest",
            "--sv-peer",
            ip + ":18444",
            "--observe-bodies",
            "--data-dir",
            "/state",
            "--api-bind",
            "0.0.0.0:8080",
            "--stratum-bind",
            "0.0.0.0:3333",
            "--payout-script",
            "51",
            "--asset-bind",
            "0.0.0.0:8090",
            "--asset-url",
            "http://127.0.0.1:8090/api/v1",
        )
        until(
            lambda: response(base + "/status")["peer"]["connected_peer_ids_seen"],
            "native handshake",
        )
        funding, (payment,) = funding_transactions(stock, 1)
        stock.rpc("sendrawtransaction", funding)
        funded = stock.rpc("generate", 1)[0]
        until(
            lambda: response(base + "/status")["mining"]["tip_hash"] == funded,
            "witnessed funding",
        )
        parent = stock.rpc("generate", 1)[0]
        until(
            lambda: response(base + "/status")["mining"]["tip_hash"] == parent,
            "funding successor",
        )
        assert (
            response(base + "/transactions", payment)["txid"]
            == dsha(payment)[::-1].hex()
        )

        def capture():
            s = response(base + "/status")["mining"]
            return {
                k: s[k]
                for k in (
                    "tip_hash",
                    "clock_step_seconds",
                    "readiness_reasons",
                    "observation_window_ready",
                    "solved_blocks",
                )
            }

        held = []
        for offset in ("+600s", "-600s"):
            with stratum_job(parent, payment, stratum, api, "clock-worker") as job:
                nonce = next(
                    i
                    for i in range(1024)
                    if int.from_bytes(dsha(job.header(i)), "little") <= REGTEST_TARGET
                )
                step(offset)
                until(
                    lambda: not capture()["observation_window_ready"],
                    "clock readiness loss",
                )
                row = capture()
                assert (
                    "wall clock differs from monotonic elapsed time"
                    in row["readiness_reasons"]
                )
                try:
                    job.submit_native(nonce)
                except (RuntimeError, OSError, ValueError, AssertionError):
                    pass
                else:
                    raise AssertionError("clock-uncertain solve accepted")
                assert (
                    stock.rpc("getbestblockhash") == parent
                    and capture()["solved_blocks"] == 0
                )
                held.append({"offset": offset, "status": row})
            step("+0s")
            until(lambda: capture()["observation_window_ready"], "clock recovery")
        with stratum_job(parent, payment, stratum, api, "recovered") as job:
            nonce = next(
                i
                for i in range(1024)
                if int.from_bytes(dsha(job.header(i)), "little") <= REGTEST_TARGET
            )
            solved = dsha(job.header(nonce))[::-1].hex()
            assert job.submit_native(nonce)
        until(
            lambda: stock.rpc("getbestblockhash") == solved,
            "stock acceptance after clock recovery",
        )
        return {
            "reference": IMAGE,
            "binary_sha256": docker(
                "exec", name, "sha256sum", "/usr/local/bin/scary-node"
            ).split()[0],
            "clock_library": docker("exec", name, "dpkg-query", "-W", "libfaketime"),
            "parent": parent,
            "held_submissions": held,
            "solution": solved,
            "reference_tip": stock.rpc("getbestblockhash"),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--image", default="scary-node:clock-lab")
    args = parser.parse_args()
    args.receipt.write_text(json.dumps(measure(args.image), indent=2) + "\n")
