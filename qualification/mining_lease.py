"""Exercise Stratum identity fencing, stale restores and legacy range migration on a release executable."""

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def process(binary, directory, root):
    api, stratum, assets = port(), port(), port()
    args = [
        str(binary),
        "--network",
        "regtest",
        "--sv-peer",
        "127.0.0.1:1",
        "--api-bind",
        f"127.0.0.1:{api}",
        "--stratum-bind",
        f"127.0.0.1:{stratum}",
        "--asset-bind",
        f"127.0.0.1:{assets}",
        "--asset-url",
        f"http://127.0.0.1:{assets}/api/v1",
        "--payout-script",
        "51",
        "--data-dir",
        str(directory),
        "--mining-lease-dir",
        str(root),
    ]
    with tempfile.TemporaryFile() as log:
        p = subprocess.Popen(args, stdout=log, stderr=log)
        try:
            yield p, log, api, stratum
        finally:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)


def subscribed(instance):
    p, log, api, stratum = instance
    for _ in range(200):
        if p.poll() is not None:
            log.seek(0)
            raise RuntimeError(log.read().decode()[-400:])
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{api}/status", timeout=1
            ) as reply:
                if json.load(reply)["network"] == "regtest":
                    break
        except OSError:
            time.sleep(0.025)
    else:
        raise RuntimeError("regtest operator endpoint unavailable")
    with socket.create_connection(("127.0.0.1", stratum), timeout=3) as stream:
        stream.sendall(b'{"id":1,"method":"mining.subscribe","params":[]}\n')
        with stream.makefile("rb") as wire:
            response = json.loads(wire.readline())
        assert response["id"] == 1 and response["result"][2] == 4
        return int(response["result"][1], 16)


def refused(instance, expected):
    p, log, _, _ = instance
    assert p.wait(timeout=5) != 0
    log.seek(0)
    assert expected in log.read().decode()
    return expected


def run(binary, temp):
    root, alternate, primary, restored = [
        temp / x for x in ("leases", "alternate", "active", "restored")
    ]
    root.mkdir()
    alternate.mkdir()
    state = primary / "regtest"
    state.mkdir(parents=True)
    (state / "extranonce.watermark").write_bytes((250).to_bytes(4, "little"))
    with process(binary, primary, root) as active:
        first = subscribed(active)
        shutil.copytree(primary, restored)
        # Snapshot the standby before burning the primary's second range.
        for _ in range(1024):
            # Only the first returned subscription waits for the API. Real TCP opens
            # allocate additional serials; read the response to account for each one.
            with socket.create_connection(
                ("127.0.0.1", active[3]), timeout=3
            ) as stream:
                stream.sendall(b'{"id":1,"method":"mining.subscribe","params":[]}\n')
                with stream.makefile("rb") as wire:
                    last = int(json.loads(wire.readline())["result"][1], 16)
        assert first == 251 and last == 1275
        with process(binary, restored, root) as duplicate:
            overlap = refused(
                duplicate, "mining identity already active or lease unavailable"
            )
        with process(binary, temp / "distinct", root) as other:
            assert subscribed(other) == 1
        # The context terminates the active process with SIGKILL.
    with process(binary, restored, alternate) as wrong:
        missing = refused(wrong, "mining lease domain changed or missing")
    with process(binary, restored, root) as next_process:
        next_serial = subscribed(next_process)
    assert next_serial == 2299
    return {
        "network": "regtest",
        "release_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "legacy_watermark": 250,
        "first_subscription": first,
        "last_primary_subscription": last,
        "fenced_concurrent_restore": overlap,
        "parallel_distinct_identity_subscribed": True,
        "shutdown": "SIGKILL",
        "rejected_empty_authority": missing,
        "first_restored_subscription": next_serial,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument(
        "--binary",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "target/release/scary-node",
    )
    args = p.parse_args()
    with tempfile.TemporaryDirectory() as d:
        result = run(args.binary.resolve(), Path(d))
    args.receipt.write_text(json.dumps(result, indent=2) + "\n")
