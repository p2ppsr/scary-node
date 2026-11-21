"""Verify role separation and the release fingerprint on the real process interfaces."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from mining_lease import port


def http(url, token=None, method="GET"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with urlopen(Request(url, headers=headers, method=method), timeout=2) as reply:
            return reply.status, reply.read()
    except HTTPError as error:
        return error.code, error.read()


def run(binary, directory):
    api, assets, worker = port(), port(), port()
    operator_secret, worker_secret = secrets.token_hex(32), secrets.token_hex(32)
    for file, secret in [("operator", operator_secret), ("worker", worker_secret)]:
        (directory / file).write_text(secret)
    base = [
        str(binary),
        "--data-dir",
        str(directory / "state"),
        "--api-bind",
        f"127.0.0.1:{api}",
        "--asset-bind",
        f"127.0.0.1:{assets}",
        "--asset-url",
        f"http://127.0.0.1:{assets}/api/v1",
        "--stratum-bind",
        f"127.0.0.1:{worker}",
        "--worker-password-file",
        str(directory / "worker"),
        "--payout-script",
        "51",
    ]
    mainnet = subprocess.run(
        base
        + [
            "--alert-service-url",
            "http://127.0.0.1:1",
            "--alert-token-file",
            str(directory / "operator"),
        ],
        capture_output=True,
        timeout=10,
    )
    assert (
        mainnet.returncode != 0 and b"requires --operator-token-file" in mainnet.stderr
    )
    digest = sha256(binary.read_bytes()).hexdigest()
    args = base + [
        "--network",
        "regtest",
        "--sv-peer",
        "127.0.0.1:1",
        "--operator-token-file",
        str(directory / "operator"),
    ]
    with subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ) as p:
        try:
            url = f"http://127.0.0.1:{api}/status"
            for _ in range(100):
                assert p.poll() is None
                try:
                    ok, raw = http(url, operator_secret)
                    if ok == 200:
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError("operator endpoint unavailable")
            assert json.loads(raw)["release"]["executable_sha256"] == digest
            anonymous = http(url)[0]
            wrong_role = http(url, worker_secret)[0]
            missing = http(f"http://127.0.0.1:{assets}/status")[0]
            readonly = http(
                f"http://127.0.0.1:{assets}/api/v1/block/" + "0" * 64, method="POST"
            )[0]
            assert anonymous == wrong_role == 401 and missing == 404 and readonly == 405
            with (
                socket.create_connection(("127.0.0.1", worker), timeout=2) as stream,
                stream.makefile("rwb") as f,
            ):

                def call(i, method, params):
                    f.write(
                        json.dumps(
                            {"id": i, "method": method, "params": params}
                        ).encode()
                        + b"\n"
                    )
                    f.flush()
                    while True:
                        message = json.loads(f.readline())
                        if message.get("id") == i:
                            return message

                assert call(1, "mining.subscribe", [])["result"][2] == 4
                denied = call(2, "mining.authorize", ["test", operator_secret])
                assert denied["error"][0] == 24
                assert (
                    call(3, "mining.authorize", ["test", worker_secret])["result"]
                    is True
                )
        finally:
            p.terminate()
    return {
        "release_sha256": digest,
        "operator_status_authenticated": ok,
        "operator_status_anonymous": anonymous,
        "operator_status_worker_credential": wrong_role,
        "asset_status_route": missing,
        "asset_block_write": readonly,
        "worker_operator_credential_error": denied["error"][0],
        "worker_authorized": True,
        "mainnet_miner_missing_operator_credential_refused": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument(
        "--binary",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "target/release/scary-node",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as d:
        args.receipt.write_text(
            json.dumps(run(args.binary.resolve(), Path(d)), indent=2) + "\n"
        )
