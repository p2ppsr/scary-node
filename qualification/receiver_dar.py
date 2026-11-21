"""Run the complete signed DAR lifecycle through the pinned receiver's native loopback sync and SQLite."""

import argparse
import contextlib
import hashlib
import gzip
import json
import os
from pathlib import Path
import signal
import subprocess
import struct
import tempfile
import urllib.error
import urllib.request
from sv_reference import RpcError
from scary_dar import ROOT, Reference, LabFeed, run, get_port, response, until, entrant


class NativeFeed(LabFeed):
    def __init__(self, node, directory, life):
        super().__init__(node, directory)
        self.server.shutdown()
        self.server.server_close()
        self.directory = directory
        self.life = life
        self.peer_port = get_port()
        self.gateway_port = get_port()
        self.started = False
        self.history = directory / "signed.json"
        self.history.write_text("[]")

    def add(self, kind, payload):
        seq = super().add(kind, payload)
        tmp = self.history.with_suffix(".next")
        tmp.write_text(json.dumps([x["raw"] for x in self.records]))
        os.replace(tmp, self.history)
        return seq

    def start(self, args, log):
        file = self.life.enter_context((self.directory / log).open("ab"))
        proc = subprocess.Popen(
            args,
            stdout=file,
            stderr=file,
            env={**os.environ, "TZ": "UTC", "ALERT_SYSTEM_ENVIRONMENT": "test"},
        )

        def stop():
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
            proc.wait(timeout=15)

        self.life.callback(stop)
        return proc

    def endpoint(self, operator_port):
        if self.started:
            return self.gateway_port
        self.started = True
        self.operator_port = operator_port
        d = self.directory
        receiver_log = d / "receiver.log"
        token = d / "token"
        self.start(
            [
                "python3",
                str(ROOT / "scripts/alert-gateway.py"),
                "--receiver",
                f"http://127.0.0.1:{self.peer_port}",
                "--node",
                f"http://127.0.0.1:{operator_port}",
                "--port",
                str(self.gateway_port),
                "--token-file",
                str(token),
                "--operator-token-file",
                str(token),
                "--receiver-sync-log",
                str(receiver_log),
            ],
            "gateway.log",
        )
        configs = []
        for role in ("publisher", "receiver"):
            c = json.loads(
                (ROOT / ".native/alert/app/config/envs/mainnet.json").read_text()
            )
            c.update(
                environment="test",
                genesis_keys=self.keys,
                alert_processing_interval="1s",
                log_output_file=str(d / (role + ".log")),
            )
            c["datastore"]["sqlite"]["database_path"] = str(d / (role + ".sqlite"))
            c["datastore"]["table_prefix"] = "alert_system"
            c["datastore"]["debug"] = False
            c["rpc_connections"] = [
                {
                    "host": f"http://127.0.0.1:{self.gateway_port}",
                    "user": "scary",
                    "password": self.token,
                }
            ]
            path = d / (role + ".json")
            path.write_text(json.dumps(c))
            configs.append(str(path))
        self.proc = self.start(
            [
                str(ROOT / ".native/receiver-lab"),
                *configs,
                str(self.history),
                f"127.0.0.1:{self.peer_port}",
            ],
            "receiver-process.log",
        )

        def started():
            if self.proc.poll() is not None:
                raise RuntimeError((d / "receiver-process.log").read_text()[-1200:])
            return response(f"http://127.0.0.1:{self.peer_port}/health")[
                "sequence"
            ] == len(self.records)

        until(started, "pinned receiver reconstructed pre-entry history")
        return self.gateway_port

    def health(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.gateway_port}/health",
            headers={"Authorization": "Bearer " + self.token},
        )
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.load(r)


def measure(binary, dir, *, retain_history=False):
    with (
        Reference().running(peer=True, fast_rpc=retain_history) as n,
        contextlib.ExitStack() as life,
    ):
        f = NativeFeed(n, dir, life)
        result = run(n, f, dir, binary, retain_history=retain_history)

        def field(b):
            assert len(b) < 253
            return bytes([len(b)]) + b

        outcomes = []
        with entrant(binary, n, f, dir, f.operator_port, get_port()) as api:

            def apply(kind, payload):
                seq = f.add(kind, payload)

                def settled():
                    health = f.health()
                    s = response(api + "/status")["mining"]
                    return (
                        s["directives"]
                        if s["durable_directive_sequence"] == seq
                        and s["directives"]["ready"]
                        and health["sequence"] == seq
                        and health["unprocessed_alerts"] == 0
                        else None
                    )

                state = until(settled, "upstream and node receipts at " + str(seq))
                outcomes.append(
                    {"kind": kind, "sequence": seq, "directive_state": state}
                )

            apply(1, field(b"isolated qualification notice"))
            address = n.rpc("getnewaddress")
            funding = n.rpc("sendtoaddress", address, 1)
            output = next(
                v
                for v in n.rpc("getrawtransaction", funding, 1)["vout"]
                if address in v["scriptPubKey"].get("addresses", [])
            )
            index = output["n"]
            for _ in range(2):
                current = n.rpc("generate", 1)[0]
                until(
                    lambda: response(api + "/status")["mining"]["tip_hash"] == current,
                    "funding observation",
                )
            spend = n.rpc(
                "signrawtransaction",
                n.rpc(
                    "createrawtransaction",
                    [{"txid": funding, "vout": index}],
                    {n.rpc("getnewaddress"): 0.9999},
                ),
            )
            assert spend["complete"]
            height = n.rpc("getblockcount")

            def interval(kind, start, stop):
                apply(
                    kind,
                    bytes.fromhex(funding)
                    + struct.pack("<QQQB", index, start, stop, 1),
                )
                reply = n.rpc(
                    "addToConsensusBlacklist",
                    {
                        "funds": [
                            {
                                "txOut": {"txId": funding, "vout": index},
                                "enforceAtHeight": [{"start": start, "stop": stop}],
                                "policyExpiresWithConsensus": True,
                            }
                        ]
                    },
                )
                assert not reply.get("notProcessed", [])

            interval(2, height, 2**31 - 1)
            try:
                response(api + "/transactions", bytes.fromhex(spend["hex"]))
            except urllib.error.HTTPError as e:
                reason = json.load(e)["error"]
                assert e.code == 422 and "frozen" in reason
            else:
                raise AssertionError("entrant admitted frozen ordinary input")
            try:
                n.rpc("sendrawtransaction", spend["hex"])
            except RpcError as e:
                stock_rejection = str(e)
                assert (
                    "frozen" in stock_rejection.lower()
                    or "blacklist" in stock_rejection.lower()
                )
            else:
                raise AssertionError("reference admitted frozen ordinary input")
            interval(3, height - 1, height)
            txid = response(api + "/transactions", bytes.fromhex(spend["hex"]))["txid"]
            assert n.rpc("sendrawtransaction", spend["hex"]) == txid
            included = n.rpc("generate", 1)[0]
            assert txid in n.rpc("getblock", included)["tx"]
            until(
                lambda: response(api + "/status")["mining"]["tip_hash"] == included,
                "released payment inclusion",
            )
            result["ordinary_freeze_unfreeze"] = {
                "funding": funding,
                "index": index,
                "entrant_rejection": reason,
                "sv_rejection": stock_rejection,
                "released_payment": txid,
                "inclusion": included,
            }

            address = field(b"127.0.0.1")
            apply(5, address + field(b"isolated peer ban"))
            until(lambda: n.rpc("getconnectioncount") == 0, "live peer ban closes TCP")
            outcomes[-1]["reference_tcp_connections"] = n.rpc("getconnectioncount")
            apply(6, address + field(b"isolated unban"))
            until(lambda: n.rpc("getconnectioncount") == 1, "unban permits redial")
            outcomes[-1]["reference_tcp_connections"] = n.rpc("getconnectioncount")
            apply(
                7,
                bytes.fromhex(result["confirmed_hash"])[::-1]
                + field(b"detached fixture branch"),
            )
            rotated = [n.rpc("getnewaddress") for _ in range(5)]
            keys = [n.rpc("validateaddress", x)["pubkey"] for x in rotated]
            apply(8, b"".join(bytes.fromhex(key) for key in keys))
            f.wallet = rotated
            apply(1, field(b"notice from the new authorized signers"))
            result["directive_lifecycle"] = outcomes
        result["signed_records"] = list(f.records)
        result["alert_source"] = (
            "upstream receiver with isolated native libp2p synchronization"
        )
        health = until(
            lambda: (
                h
                if (h := f.health())["sequence"] == len(f.records)
                and h["unprocessed_alerts"] == 0
                and h["peer_sync"].get("sequence") == len(f.records)
                else None
            ),
            "all upstream RPC actions durably acknowledged",
        )
        result["receiver"] = {
            "release": "v0.3.5",
            "source_commit": "c19f9959fb979f9def944807bd4e58ecadf25717",
            "transport": "two loopback libp2p hosts, native StreamThread sync, independent SQLite stores",
            "sequence": health["sequence"],
            "active_peers": health["active_peers"],
            "unprocessed_alerts": health["unprocessed_alerts"],
            "last_peer_sync": health["peer_sync"],
            "harness_sha256": hashlib.sha256(
                (ROOT / ".native/receiver-lab").read_bytes()
            ).hexdigest(),
        }
        if retain_history:
            tip = n.rpc("getblockchaininfo")
            if tip["chain"] != "regtest":
                raise ValueError("the lab can export only regtest fixture history")
            result["active_history_from_height_one"] = [
                n.rpc("getblock", n.rpc("getblockhash", height), 0)
                for height in range(1, tip["blocks"] + 1)
            ]
            result["sv_final_active_tip"] = tip["bestblockhash"]
            result["node_binary_sha256"] = hashlib.sha256(
                binary.read_bytes()
            ).hexdigest()
        return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/scary-node")
    p.add_argument(
        "--retain-history",
        action="store_true",
        help="Export both exact regtest branches and the independent SV maturity response.",
    )
    a = p.parse_args()
    with tempfile.TemporaryDirectory() as td:
        try:
            r = measure(a.binary, Path(td), retain_history=a.retain_history)
        except Exception:
            for name in [
                "receiver-process.log",
                "receiver.log",
                "node.log",
                "gateway.log",
            ]:
                path = Path(td) / name
                if path.exists():
                    print(path.read_text()[-700:])
            raise
    raw = (json.dumps(r, indent=2) + "\n").encode()
    tmp = a.receipt.with_suffix(a.receipt.suffix + ".next")
    tmp.write_bytes(gzip.compress(raw, mtime=0) if a.receipt.suffix == ".gz" else raw)
    tmp.replace(a.receipt)
