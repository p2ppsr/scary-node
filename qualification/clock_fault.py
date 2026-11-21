"""Step only the entrant's wall clock on an isolated regtest and check an actual Stratum job."""

import argparse
import contextlib
import json
import socket
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


def response_available(base):
    try:
        return bool(response(base + "/status"))
    except OSError:
        return False


def observe_withheld(port):
    # An independently authenticated regtest worker asks through the real socket.
    with socket.create_connection(("127.0.0.1", port), timeout=4) as sock:
        sock.sendall(
            b'{"id":1,"method":"mining.subscribe","params":[]}\n{"id":2,"method":"mining.authorize","params":["clock-probe","x"]}\n'
        )
        sock.settimeout(1.25)
        data = b""
        while True:
            try:
                part = sock.recv(4096)
            except socket.timeout:
                break
            if not part:
                break
            data += part
        messages = [json.loads(row) for row in data.splitlines()]
        assert any(m.get("id") == 2 and m["result"] is True for m in messages)
        assert all(m.get("method") != "mining.notify" for m in messages)
        return {"authorized": True, "jobs_received": 0}


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

        def start():
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

        def stop():
            # This ephemeral image runs one node and the qualification init process.
            docker(
                "exec",
                name,
                "sh",
                "-c",
                'for p in /proc/[0-9]*; do if [ "$(cat "$p/comm" 2>/dev/null)" = scary-node ]; then kill -TERM "${p##*/}"; fi; done',
            )
            until(lambda: not response_available(base), "stopped isolated entrant")

        step("+0s")
        start()
        until(
            lambda: response(base + "/status")["peer"]["connected_peer_ids_seen"],
            "native handshake",
        )
        funding, (payment, boot_payment) = funding_transactions(stock, 2)
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
                    "peer_clock",
                    "pending_transactions",
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
        until(lambda: capture()["tip_hash"] == solved, "persisted first solution")
        boot_holds = []
        for offset, seconds in (("+600s", 600), ("-600s", -600)):
            stop()
            step(offset)
            start()
            until(
                lambda: (
                    response_available(base)
                    and capture()["peer_clock"]["connected_endpoints"] >= 1
                    and capture()["tip_hash"] == solved
                ),
                "startup peer-time comparison",
            )
            assert (
                response(base + "/transactions", boot_payment)["txid"]
                == dsha(boot_payment)[::-1].hex()
            )
            row = capture()
            assert abs(row["clock_step_seconds"]) <= 2
            assert abs(row["peer_clock"]["offset_from_wall_seconds"] + seconds) < 15
            assert row["pending_transactions"] and not row["observation_window_ready"]
            assert (
                "SV peer times disagree with local clock or each other"
                in row["readiness_reasons"]
            )
            assert stock.rpc("getbestblockhash") == solved
            boot_holds.append(
                {
                    "offset": offset,
                    "status": row,
                    "worker_observation": observe_withheld(stratum),
                }
            )
        stop()
        step("+0s")
        start()
        until(
            lambda: response_available(base) and capture()["observation_window_ready"],
            "restored synchronized host clock",
        )
        assert (
            response(base + "/transactions", boot_payment)["txid"]
            == dsha(boot_payment)[::-1].hex()
        )
        with stratum_job(
            solved, boot_payment, stratum, api, "synchronized-start"
        ) as job:
            winning = next(
                i
                for i in range(1024)
                if int.from_bytes(dsha(job.header(i)), "little") <= REGTEST_TARGET
            )
            boot_solved = dsha(job.header(winning))[::-1].hex()
            assert job.submit_native(winning)
        until(
            lambda: stock.rpc("getbestblockhash") == boot_solved,
            "stock node selected solution after synchronized startup",
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
            "reference_tip": solved,
            "startup_holds": boot_holds,
            "synchronized_start_solution": boot_solved,
            "final_reference_tip": stock.rpc("getbestblockhash"),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--image", default="scary-node:clock-lab")
    args = parser.parse_args()
    args.receipt.write_text(json.dumps(measure(args.image), indent=2) + "\n")
