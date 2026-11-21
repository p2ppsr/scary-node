"""Fill a bounded disposable state volume, verify fail-stop and recover from native peers."""

import argparse
import contextlib
import json
import subprocess
import uuid
from pathlib import Path

from scary_dar import response, until
from sv_reference import IMAGE, Reference


def run(image, interrupt=False):
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
                '"$@" > /tmp/observer.log 2>&1 & child=$!; echo "$child" > /tmp/observer.pid; wait "$child"; echo $? > /tmp/exit',
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

        def restored_snapshot():
            row = next(
                json.loads(line)
                for line in shell("cat", "/tmp/observer.log").splitlines()
                if line.startswith('{"')
                and json.loads(line).get("event") == "witness_restored"
            )
            return {
                k: row[k]
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
        restored = restored_snapshot()
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
        interrupted = None
        if interrupt:
            from native_workload import solve
            from chronicle import CTransaction, CTxOut, FromHex, ToHex
            from test_framework.script import CScript, OP_FALSE, OP_RETURN

            address = full.rpc("getnewaddress")
            funding = full.rpc("sendtoaddress", address, 1)
            parent = full.rpc("generate", 1)[0]
            until(lambda: state()["tip_hash"] == parent, "funding checkpoint")
            output = next(
                v["n"]
                for v in full.rpc("getrawtransaction", funding, 1)["vout"]
                if v["value"] == 1
            )
            draft = full.rpc(
                "createrawtransaction",
                [{"txid": funding, "vout": output}],
                {address: 0.99},
            )
            pay = FromHex(CTransaction(), draft)
            pay.vout.append(CTxOut(0, CScript([OP_FALSE, OP_RETURN, bytes(256 << 10)])))
            signed = full.rpc("signrawtransaction", ToHex(pay))
            assert signed["complete"]
            pay = FromHex(CTransaction(), signed["hex"])
            height = full.rpc("getblockcount") + 1
            body = solve(
                parent, height, full.rpc("getblock", parent)["time"] + 1, [pay]
            )
            previous_digest = shell("sha256sum", committed).split()[0]
            temp = "/state/regtest/witness.next"
            shell("mkfifo", temp)
            docker(
                "exec",
                "-d",
                name,
                "sh",
                "-c",
                'exec 3<"$1"; head -c 4096 <&3 > /tmp/written; touch /tmp/write-started; sleep 60',
                "slow-reader",
                temp,
            )
            assert (
                full.rpc("submitblock", body.serialize().hex()) is None
                and full.rpc("getbestblockhash") == body.hash
            )
            until(
                lambda: shell(
                    "sh", "-c", "test -e /tmp/write-started && echo yes || true"
                ),
                "checkpoint write started",
            )
            shell("sh", "-c", 'kill -KILL "$(cat /tmp/observer.pid)"')
            assert until(exited, "terminated during serialization") == 137
            actual = shell("stat", "-c", "%s", "/tmp/written")
            assert (
                int(actual) == 4096
                and shell("sha256sum", committed).split()[0] == previous_digest
            )
            shell("rm", temp, "/tmp/exit")
            shell("cp", "/tmp/written", temp)
            start()
            checkpoint = restored_snapshot()
            assert checkpoint["tip_hash"] == parent
            next_hash = full.rpc("generate", 1)[0]
            until(
                lambda: state()["tip_hash"] == next_hash,
                "replayed interrupted large body",
            )
            last = response(api)["mining"]["validation_window"]
            row = next(x for x in last if x["hash"] == body.hash)
            assert row["script_checks_passed"] == 1
            interrupted = {
                "signal": "SIGKILL",
                "bytes_read_from_inflight_checkpoint": int(actual),
                "previous_checkpoint_sha256": previous_digest,
                "restored_parent": parent,
                "interrupted_block": body.hash,
                "replayed_known_signatures": row["script_checks_passed"],
                "repaired": state(),
                "reference_tip": next_hash,
            }
        return {
            **({"interrupted_write": interrupted} if interrupted else {}),
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
    parser.add_argument(
        "--interrupt",
        action="store_true",
        help="kill a live writer after 4 KiB and replay its signed body",
    )
    args = parser.parse_args()
    args.receipt.write_text(
        json.dumps(run(args.image, args.interrupt), indent=2) + "\n"
    )
