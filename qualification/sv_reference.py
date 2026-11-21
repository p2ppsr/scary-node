"""Pinned, network-isolated SV Node process for contemporary consensus tests."""

import contextlib
import json
import subprocess
import time
import uuid

IMAGE = "bitcoinsv/bitcoin-sv:1.2.3@sha256:d96fe4786a00e5fc10bc9e0e76a6006376f9472d4eb74d07e4c1e0d019493d33"


class RpcError(RuntimeError):
    pass


class Reference:
    def __init__(self):
        self.container = "scary-qual-" + uuid.uuid4().hex

    def docker(self, *args):
        return subprocess.run(
            ["docker", *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def rpc(self, method, *args):
        encoded = [arg if isinstance(arg, str) else json.dumps(arg) for arg in args]
        command = [
            "docker",
            "exec",
            self.container,
            "bitcoin-cli",
            "-regtest",
            "-datadir=/data",
            method,
            *encoded,
        ]
        reply = subprocess.run(command, capture_output=True, text=True)
        if reply.returncode:
            raise RpcError(reply.stderr.strip())
        value = reply.stdout.strip()
        if not value:
            return None
        try:
            return json.loads(value)
        except ValueError:
            return value

    def ready(self):
        for _ in range(120):
            try:
                if self.rpc("getblockchaininfo")["chain"] == "regtest":
                    return
            except RpcError:
                time.sleep(0.25)
        raise RuntimeError(self.docker("logs", self.container)[-1500:])

    def restart(self):
        self.docker("restart", "--time", "60", self.container)
        self.ready()

    @contextlib.contextmanager
    def running(self, peer=False):
        networking = (
            ["--network", "bridge", "--publish", "127.0.0.1::18444"]
            if peer
            else ["--network", "none"]
        )
        self.docker(
            "run",
            "--detach",
            "--platform",
            "linux/amd64",
            *networking,
            "--name",
            self.container,
            "--volume",
            "/data",
            "--entrypoint",
            "bitcoind",
            IMAGE,
            "-regtest",
            "-datadir=/data",
            "-server",
            f"-listen={int(peer)}",
            "-dnsseed=0",
            "-discover=0",
            "-genesisactivationheight=1",
            "-chronicleactivationheight=2",
            "-excessiveblocksize=20000000",
            "-maxstackmemoryusageconsensus=100000000",
            "-txindex=1",
            "-acceptnonstdtxn=1",
            "-minminingtxfee=0.00000500",
        )
        try:
            self.ready()
            yield self
        finally:
            self.docker("rm", "--force", "--volumes", self.container)
