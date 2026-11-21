"""Signed DAR lifecycle through Scary Node and a live, independently queried SV Node."""

import argparse
import base64
import contextlib
import hashlib
import json
import os
import secrets
import signal
import socket
import sys
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sv_reference import Reference, IMAGE, RpcError
from sv_dar import confiscate, require

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "regtest"))
from race_work import REGTEST_TARGET, stratum_job
from prove import dsha


def mine_with_worker(api, port, stratum, node, payment):
    parent = node.rpc("getbestblockhash")
    with stratum_job(parent, payment, stratum, port) as job:
        nonce = next(
            i
            for i in range(1024)
            if int.from_bytes(dsha(job.header(i)), "little") <= REGTEST_TARGET
        )
        result = dsha(job.header(nonce))[::-1].hex()
        job.submit_native(nonce)
    until(
        lambda: node.rpc("getbestblockhash") == result,
        "independent SV adoption of Scary solution",
    )
    assert node.rpc("getblock", result)["tx"][1] == dsha(payment)[::-1].hex()
    return result


def until(f, describe, timeout=50):
    start = time.monotonic()
    last = None
    while time.monotonic() - start < timeout:
        try:
            last = f()
            if last:
                return last
        except (OSError, ValueError) as exc:
            last = str(exc)
        time.sleep(0.08)
    raise RuntimeError(f"timed out waiting for {describe} ({last})")


def empty_successor(node):
    """Mine a valid coinbase-only alternate block without selecting wallet mempool entries."""
    parent = node.rpc("getblock", node.rpc("getbestblockhash"))
    height = parent["height"] + 1
    encoded = height.to_bytes((height.bit_length() + 7) // 8, "little")
    if encoded[-1] & 128:
        encoded += bytes(1)
    script = bytes([len(encoded)]) + encoded + b"scary-regtest"
    coinbase = (
        struct.pack("<I", 1)
        + b"\x01"
        + bytes(32)
        + b"\xff" * 4
        + bytes([len(script)])
        + script
        + b"\xff" * 4
        + b"\x01"
        + bytes(8)
        + b"\x01\x51"
        + bytes(4)
    )
    prefix = (
        struct.pack("<I", 0x20000000)
        + bytes.fromhex(parent["hash"])[::-1]
        + dsha(coinbase)
        + struct.pack("<II", parent["time"] + 1, 0x207FFFFF)
    )
    for nonce in range(1024):
        header = prefix + struct.pack("<I", nonce)
        if int.from_bytes(dsha(header), "little") <= REGTEST_TARGET:
            break
    else:
        raise RuntimeError("regtest nonce search exhausted")
    block_hash = dsha(header)[::-1].hex()
    require(
        node.rpc("submitblock", (header + b"\x01" + coinbase).hex()) is None,
        "alternate block rejected",
    )
    require(node.rpc("getbestblockhash") == block_hash, "alternate block missing")
    return block_hash


def get_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def response(url, body=None):
    req = urllib.request.Request(url, data=body)
    with urllib.request.urlopen(req, timeout=4) as r:
        return json.load(r)


class LabFeed:
    def __init__(self, node, dir):
        self.node = node
        self.wallet = [node.rpc("getnewaddress") for _ in range(5)]
        self.keys = [node.rpc("validateaddress", k)["pubkey"] for k in self.wallet]
        self.records = []
        self.token = secrets.token_hex(24)
        (dir / "keys.json").write_text(json.dumps(self.keys))
        (dir / "token").write_text(self.token)
        feed = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("Authorization") != f"Bearer {feed.token}":
                    self.send_error(401)
                    return
                if self.path == "/health":
                    data = {"active_peers": 1, "sequence": len(feed.records)}
                elif self.path == "/alerts":
                    data = {
                        "alerts": [
                            {
                                "sequence_number": 0,
                                "raw": (ROOT / "qualification/alert-genesis.hex")
                                .read_text()
                                .strip(),
                            },
                            *feed.records,
                        ],
                        "latest_sequence": len(feed.records),
                    }
                else:
                    self.send_error(404)
                    return
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                feed.write_response(self.path, raw, self.wfile)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def endpoint(self, operator_port):
        return self.server.server_port

    def write_response(self, _path, raw, stream):
        stream.write(raw)

    def prepare(self, kind, payload):
        seq = len(self.records) + 1
        signed = struct.pack("<IIQI", 1, seq, int(time.time()), kind) + payload
        frame = signed + b"".join(
            base64.b64decode(self.node.rpc("signmessage", key, signed.hex()))
            for key in self.wallet[:3]
        )
        return {"sequence_number": seq, "raw": frame.hex()}

    def add(self, kind, payload):
        record = self.prepare(kind, payload)
        self.records.append(record)
        return record["sequence_number"]


@contextlib.contextmanager
def entrant(
    binary, node, feed, dir, port, stratum, *, extra_args=(), require_directives=True,
    asset_port=None, stop_signal=signal.SIGINT, exit_receipt=None,
):
    peer = node.docker("port", node.container, "18444/tcp")
    log = (dir / "node.log").open("ab")
    assets = get_port() if asset_port is None else asset_port
    p = subprocess.Popen(
        [
            str(binary),
            "--network",
            "regtest",
            "--sv-peer",
            peer,
            "--observe-bodies",
            "--data-dir",
            str(dir / "state"),
            "--stratum-bind",
            f"127.0.0.1:{stratum}",
            "--asset-bind",
            f"127.0.0.1:{assets}",
            "--asset-url",
            f"http://127.0.0.1:{assets}/api/v1",
            "--payout-script",
            "51",
            "--api-bind",
            f"127.0.0.1:{port}",
            "--alert-service-url",
            f"http://127.0.0.1:{feed.endpoint(port)}",
            "--alert-token-file",
            str(dir / "token"),
            "--regtest-alert-keys",
            str(dir / "keys.json"),
            *extra_args,
        ],
        stdout=log,
        stderr=log,
    )
    try:
        api = f"http://127.0.0.1:{port}"

        def ready():
            if p.poll() is not None:
                raise RuntimeError((dir / "node.log").read_text()[-900:])
            s = response(api + "/status")
            return s["peer"]["connected_peer_ids_seen"] and (
                not require_directives or s["mining"]["directives"]["ready"]
            )

        until(ready, "connected SV peer and signed feed")
        yield api
    finally:
        if p.poll() is None:
            p.send_signal(stop_signal)
        exit_code = p.wait(timeout=10)
        if exit_receipt is not None:
            exit_receipt.update(signal=stop_signal.name, exit_code=exit_code)
        log.close()


def run(node, feed, dir, binary, *, retain_history=False):
    rpc = node.rpc
    rpc("generate", 105)
    source_address = rpc("getnewaddress")
    recipient = rpc("getnewaddress")
    source = rpc("sendtoaddress", source_address, 1)
    outputs = rpc("getrawtransaction", source, 1)["vout"]
    index = next(
        o["n"]
        for o in outputs
        if source_address in o["scriptPubKey"].get("addresses", [])
    )
    raw = confiscate(
        source, index, bytes.fromhex(rpc("validateaddress", recipient)["scriptPubKey"])
    )
    ctx = rpc("decoderawtransaction", raw)["txid"]
    start = rpc("getblockcount") + 3
    block_source = bytes.fromhex(source) + struct.pack(
        "<QQQB", index, start, 2**31 - 1, 0
    )
    feed.add(2, block_source)
    rpc(
        "addToConsensusBlacklist",
        {
            "funds": [
                {
                    "txOut": {"txId": source, "vout": index},
                    "enforceAtHeight": [{"start": start, "stop": 2**31 - 1}],
                    "policyExpiresWithConsensus": False,
                }
            ]
        },
    )
    port = get_port()
    stratum = get_port()

    def tip(api, height, hash):
        return until(
            lambda: (s := response(api + "/status")["mining"])["height"] == height
            and s["tip_hash"] == hash,
            "active tip " + str(height),
        )

    with entrant(binary, node, feed, dir, port, stratum) as api:
        h = rpc("generate", 1)[0]
        tip(api, start - 2, h)
        h = rpc("generate", 1)[0]
        tip(api, start - 1, h)
        require(
            response(api + "/status")["mining"]["directives"]["last_applied"] == 1,
            "pre-entry alert missing",
        )
        data = bytes.fromhex(raw)
        size = (
            bytes([len(data)])
            if len(data) < 253
            else b"\xfd" + struct.pack("<H", len(data))
        )
        feed.add(4, struct.pack("<Q", start) + size + data)
        rpc(
            "addToConfiscationTxidWhitelist",
            {
                "confiscationTxs": [
                    {"confiscationTx": {"enforceAtHeight": start, "hex": raw}}
                ]
            },
        )
        until(
            lambda: (
                response(api + "/status")["mining"]["directives"]["last_applied"] == 2
            ),
            "confiscation authorization",
        )
        require(
            response(api + "/transactions", data)["txid"] == ctx,
            "entrant rejected authorized confiscation",
        )
        h_parent = rpc("getbestblockhash")
        ctx_block = mine_with_worker(api, port, stratum, node, data)
        child = rpc("generate", 1)[0]
        tip(api, start + 1, child)
        require(
            response(api + f"/outpoints/{ctx}/1")["known"],
            "missing witnessed recovered output",
        )
    with entrant(binary, node, feed, dir, port, stratum) as api:
        tip(api, start + 1, child)
        draft = rpc(
            "createrawtransaction",
            [{"txid": ctx, "vout": 1}],
            {rpc("getnewaddress"): 0.9998},
        )
        signed = rpc("signrawtransaction", draft)
        require(signed["complete"], "recipient signing failed")
        for _ in range(142):
            hashes = rpc("generate", 7)
            tip(api, rpc("getblockcount"), hashes[-1])
        hashes = rpc("generate", 3)
        tip(api, start + 998, hashes[-1])
        immature = response(api + "/check-transaction", bytes.fromhex(signed["hex"]))
        require(
            immature["outcome"] == "invalid"
            and "immature confiscation" in immature["reason"],
            "premature spend passed",
        )
        history = {}
        if retain_history:
            try:
                rpc("sendrawtransaction", signed["hex"])
            except RpcError as exc:
                require("premature-spend-of-confiscation" in str(exc), str(exc))
                history["sv_maturity_minus_one"] = {
                    "candidate_height": start + 999,
                    "parent": rpc("getbestblockhash"),
                    "response": str(exc),
                }
            else:
                raise RuntimeError("SV Node admitted the immature recipient spend")
        h = rpc("generate", 1)[0]
        tip(api, start + 999, h)
        mature = response(api + "/transactions", bytes.fromhex(signed["hex"]))
        spend_id = rpc("decoderawtransaction", signed["hex"])["txid"]
        require(mature["txid"] == spend_id, "node/reference mature spend differs")
        spend_block = mine_with_worker(
            api, port, stratum, node, bytes.fromhex(signed["hex"])
        )
        successors = rpc("generate", 2)
        final = successors[-1]
        if retain_history:
            history["detached_recipient_branch"] = [
                rpc("getblock", block_hash, 0) for block_hash in [spend_block, *successors]
            ]
        tip(api, start + 1002, final)
        require(
            spend_id in rpc("getblock", spend_block)["tx"],
            "spend missing from SV Node chain",
        )
        require(
            response(api + f"/outpoints/{ctx}/1")["consumed"],
            "Scary did not observe the confirmed spend",
        )
        before_reorg = response(api + "/status")["mining"]["solutions"]
        require(
            all(s["chain_state"] == "settled" for s in before_reorg)
            and len(before_reorg) == 2,
            "settlement missing",
        )
        rpc("invalidateblock", spend_block)
        alternate = [empty_successor(node) for _ in range(4)]
        tip(api, start + 1003, alternate[-1])
        require(
            response(api + f"/outpoints/{ctx}/1")["known"],
            "reorg failed to restore recovered outpoint",
        )
        after_reorg = response(api + "/status")["mining"]["solutions"]
        require(
            after_reorg[-1]["chain_state"] == "stale_at_depth",
            "previous solution did not lose settlement",
        )
        final = rpc("generate", 1)[0]
        tip(api, start + 1004, final)
        require(
            spend_id in rpc("getblock", final)["tx"],
            "reference did not remine recipient spend",
        )
        require(
            response(api + f"/outpoints/{ctx}/1")["consumed"],
            "entrant missed re-inclusion",
        )
        return {
            **history,
            "reference": IMAGE,
            "confiscation_block_hex": rpc("getblock", ctx_block, 0),
            "recipient_block_hex": rpc("getblock", spend_block, 0),
            "confiscation_parent": h_parent,
            "recipient_parent": h,
            "confiscation_hex": raw,
            "recipient_spend_hex": signed["hex"],
            "alternate_hashes": alternate,
            "settlement_before_reorg": before_reorg,
            "reorg_solutions": after_reorg,
            "alert_source": "isolated authenticated delivery fixture, three distinct SV Node wallet signers",
            "genesis_keys": feed.keys,
            "signed_records": feed.records,
            "confiscation_block": ctx_block,
            "confiscation_txid": ctx,
            "maturity_minus_one": immature,
            "maturity_height": start + 1000,
            "recipient_spend": spend_id,
            "confiscation_and_recipient_spend_origin": "Scary Node Stratum worker",
            "confirmed_hash": spend_block,
            "remined_spend_hash": final,
            "solutions": response(api + "/status")["mining"]["solutions"],
            "scary_active_tip": response(api + "/status")["mining"]["tip_hash"],
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--binary", type=Path, default=ROOT / "target/debug/scary-node")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as d, Reference().running(peer=True) as node:
        dir = Path(d)
        feed = LabFeed(node, dir)
        try:
            result = run(node, feed, dir, args.binary)
        except Exception:
            if (dir / "node.log").exists():
                print((dir / "node.log").read_text()[-3500:])
            raise
        finally:
            feed.server.shutdown()
            feed.server.server_close()
    args.receipt.write_text(json.dumps(result, indent=2) + "\n")
