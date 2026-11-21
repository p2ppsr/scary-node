"""Pinned, network-isolated SV Node process for contemporary consensus tests."""

import base64
import contextlib
import json
import subprocess
import time
import uuid
import urllib.request
import urllib.error

IMAGE = "bitcoinsv/bitcoin-sv:1.2.3@sha256:d96fe4786a00e5fc10bc9e0e76a6006376f9472d4eb74d07e4c1e0d019493d33"


class RpcError(RuntimeError):
    pass


class Reference:
    def __init__(self):
        self.container = "scary-qual-" + uuid.uuid4().hex
        self.http = None
        self.cli_port = []

    def docker(self, *args):
        return subprocess.run(
            ["docker", *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def rpc(self, method, *args):
        if self.http:
            url, basic = self.http
            request = urllib.request.Request(
                url,
                data=json.dumps(
                    {"id": 1, "method": method, "params": list(args)}
                ).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Basic " + basic,
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    message = json.load(response)
            except urllib.error.HTTPError as error:
                raise RpcError(error.read().decode()) from error
            if message["error"] is not None:
                raise RpcError(str(message["error"]))
            return message["result"]
        encoded = [arg if isinstance(arg, str) else json.dumps(arg) for arg in args]
        command = [
            "docker",
            "exec",
            self.container,
            "bitcoin-cli",
            "-regtest",
            "-datadir=/data",
            *self.cli_port,
            method,
            *encoded,
        ]
        data = None
        if sum(map(len, encoded)) > 32768:
            command = [
                "docker",
                "exec",
                "-i",
                self.container,
                "bitcoin-cli",
                "-regtest",
                "-datadir=/data",
                *self.cli_port,
                "-stdin",
                method,
            ]
            data = "\n".join(encoded) + "\n"
        reply = subprocess.run(command, input=data, capture_output=True, text=True)
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
    def running(
        self,
        peer=False,
        network="bridge",
        fast_rpc=False,
        *,
        block_bytes=20_000_000,
        tmpfs_bytes=None,
    ):
        networking = (
            ["--network", network, "--publish", "127.0.0.1::18444"]
            if peer
            else ["--network", "none"]
        )
        rpc_bind = []
        if fast_rpc:
            if not peer:
                raise ValueError("local HTTP RPC requires the lab bridge")
            networking += ["--publish", "127.0.0.1::18443"]
            self.cli_port = ["-rpcport=18443"]
            rpc_bind = ["-rpcbind=0.0.0.0", "-rpcallowip=0.0.0.0/0", "-rpcport=18443"]
        self.docker(
            "run",
            "--detach",
            "--platform",
            "linux/amd64",
            *networking,
            "--name",
            self.container,
            *(
                ["--volume", "/data"]
                if tmpfs_bytes is None
                else ["--tmpfs", f"/data:size={tmpfs_bytes}"]
            ),
            "--entrypoint",
            "bitcoind",
            IMAGE,
            "-regtest",
            "-datadir=/data",
            "-server",
            *rpc_bind,
            f"-listen={int(peer)}",
            "-dnsseed=0",
            "-discover=0",
            "-genesisactivationheight=1",
            "-chronicleactivationheight=2",
            f"-excessiveblocksize={block_bytes}",
            "-maxstackmemoryusageconsensus=100000000",
            "-txindex=1",
            "-acceptnonstdtxn=1",
            "-minminingtxfee=0.00000500",
        )
        try:
            self.ready()
            if fast_rpc:
                cookie = self.docker(
                    "exec", self.container, "cat", "/data/regtest/.cookie"
                )
                port = self.docker("port", self.container, "18443/tcp")
                self.http = (
                    "http://" + port,
                    base64.b64encode(cookie.encode()).decode(),
                )
            yield self
        finally:
            self.docker("rm", "--force", "--volumes", self.container)
