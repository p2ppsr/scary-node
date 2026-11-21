"""Fill a bounded disposable state volume, verify fail-stop and recover from native peers."""

import argparse
import contextlib
import json
import subprocess
import uuid
from pathlib import Path

from scary_dar import response, until
from sv_reference import IMAGE, Reference


def run(image):
    full = Reference()
    name = "scary-storage-" + uuid.uuid4().hex
    bridge = name + "-net"
    docker = full.docker
    with contextlib.ExitStack() as life:
        docker("network", "create", bridge)
        life.callback(docker, "network", "rm", bridge)
        life.enter_context(full.running(peer=True, network=bridge, fast_rpc=True))
        full.rpc("generate", 105)
        docker(
            "run",
            "-d",
            "--network",
            bridge,
            "--name",
            name,
            "--tmpfs",
            "/state:rw,size=8m",
            "-p",
            "127.0.0.1::8080",
            "--entrypoint",
            "sleep",
            image,
            "infinity",
        )
        life.callback(docker, "rm", "-fv", name)
        api = "http://" + docker("port", name, "8080/tcp") + "/status"
        ip = docker(
            "inspect",
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            full.container,
        )
        argv = [
            "scary-node",
            "--network",
            "regtest",
            "--sv-peer",
            ip + ":18444",
            "--observe-bodies",
            "--api-bind",
            "0.0.0.0:8080",
            "--data-dir",
            "/state",
        ]

        def shell(*args):
            return docker("exec", name, *args)

        def start():
            docker(
                "exec",
                "-d",
                name,
                "sh",
                "-c",
                '"$@" > /tmp/observer.log 2>&1; echo $? > /tmp/exit',
                "observer",
                *argv,
            )
            until(
                lambda: response(api)["peer"]["connected_peer_ids_seen"],
                "native handshake",
            )

        def state():
            m = response(api)["mining"]
            return {
                k: m[k]
                for k in (
                    "height",
                    "tip_hash",
                    "witnessed_outputs",
                    "observation_window_ready",
                )
            }

        start()
        tip = full.rpc("generate", 1)[0]
        until(lambda: state()["tip_hash"] == tip, "first durable boundary")
        before = state()
        committed = "/state/regtest/witness.state"
        digest = shell("sha256sum", committed).split()[0]
        # Check a live competing writer with the same identity before filling the volume.
        twin = subprocess.run(
            ["docker", "exec", name, *argv], capture_output=True, text=True, timeout=5
        )
        assert twin.returncode and "node identity already in use" in twin.stderr
        full_disk = subprocess.run(
            [
                "docker",
                "exec",
                name,
                "dd",
                "if=/dev/zero",
                "of=/state/fill",
                "bs=65536",
                "count=256",
            ],
            capture_output=True,
            text=True,
        )
        assert full_disk.returncode and "No space left on device" in full_disk.stderr
        missed = full.rpc("generate", 1)[0]

        def exited():
            value = shell("sh", "-c", "cat /tmp/exit 2>/dev/null || true")
            return int(value) if value else None

        exit_status = until(exited, "process exits on a failed checkpoint")
        log = shell("cat", "/tmp/observer.log")
        assert "No space left on device" in log and "committing witnessed branch" in log
        assert shell("sha256sum", committed).split()[0] == digest
        shell("rm", "/state/fill", "/tmp/exit")
        # An interrupted writer can leave arbitrary bytes under the temporary name.
        shell(
            "sh",
            "-c",
            'head -c 53 "$1" > /state/regtest/witness.next',
            "partial",
            committed,
        )
        partial_size = int(shell("stat", "-c", "%s", "/state/regtest/witness.next"))
        start()
        restored = state()
        assert (
            restored["tip_hash"] == before["tip_hash"]
            and restored["witnessed_outputs"] == before["witnessed_outputs"]
        )
        successor = full.rpc("generate", 1)[0]
        until(
            lambda: state()["tip_hash"] == successor,
            "replay missed ancestor over native TCP",
        )
        repaired = state()
        assert (
            repaired["observation_window_ready"] and repaired["witnessed_outputs"] == 3
        )
        assert full.rpc("getblockhash", before["height"] + 1) == missed
        return {
            "image_id": docker("inspect", "--format", "{{.Image}}", name),
            "reference": IMAGE,
            "binary_sha256": shell("sha256sum", "/usr/local/bin/scary-node").split()[0],
            "volume_bytes": 8 << 20,
            "reference_tip": successor,
            "missed_parent": missed,
            "fault": "ENOSPC on checkpoint write",
            "process_exit": exit_status,
            "committed_sha256_preserved": digest,
            "incomplete_temporary_bytes": partial_size,
            "concurrent_writer_refused": True,
            "before": before,
            "restored": restored,
            "repaired": repaired,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--image", default="scary-node:local")
    args = parser.parse_args()
    args.receipt.write_text(json.dumps(run(args.image), indent=2) + "\n")
