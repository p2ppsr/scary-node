"""Prepare real two-transaction regtest jobs and race equal nonce lanes at one receiver.

The caller admits the same signed payment to both miners, fixes the starting
nonce before requesting work, and supplies the neutral node's proposal and
submitblock RPC calls. Production nodes and mining defaults are untouched.
"""
import http.client
import json
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from prove import AUTH, dsha, parse_tx, request, rpc, txid, wait
from verify_receipt import require, single_input

REGTEST_BITS = 0x207fffff
REGTEST_TARGET = 0x7fffff << 232
MAX_TICKS = 64


@dataclass
class Job:
    label: str
    kind: str
    job_id: str
    parent: str
    prefix: bytes
    coinbase: bytes
    payment: bytes
    _submit: Callable[[int], object]

    def header(self, nonce):
        return self.prefix + struct.pack("<I", nonce)

    def submit_native(self, nonce):
        """Align the selected producer after measuring the neutral receiver's outcome."""
        require(int.from_bytes(dsha(self.header(nonce)), "little") <= REGTEST_TARGET,
                "native alignment requires the selected network-target solution")
        return self._submit(nonce)


def checked_job(label, kind, job_id, parent, version, timestamp, bits, coinbase, payment, submit):
    require(bits == REGTEST_BITS, "ordinary regtest target required")
    previous = bytes.fromhex(parent)
    require(len(previous) == 32 and isinstance(payment, bytes), "expected a parent hash and payment bytes")
    for raw in (coinbase, payment):
        _, _, end = parse_tx(raw, 0)
        require(end == len(raw), "trailing bytes in job transaction")
    prev_txid, prev_index, script = single_input(coinbase)
    require(prev_txid == "00" * 32 and prev_index == 0xffffffff and 2 <= len(script) <= 100,
            "job coinbase input mismatch")
    merkle = dsha(dsha(coinbase) + dsha(payment))
    prefix = struct.pack("<I", version) + previous[::-1] + merkle + struct.pack("<II", timestamp, bits)
    return Job(label, kind, job_id, parent, prefix, coinbase, payment, submit)


@contextmanager
def teranode_job(parent, payment, node, label="full"):
    """Use Teranode v0.16.1's retained candidate coinbase and display-order Merkle proof."""
    chain = rpc(node, "getblockchaininfo")
    require(chain["chain"] == "regtest" and chain["bestblockhash"] == parent,
            "Teranode is not on the selected regtest parent")
    payment_id = txid(payment)

    def prepared():
        job = rpc(node, "getminingcandidate", True)
        if job["prevhash"] != parent:
            raise RuntimeError("Teranode candidate parent changed")
        return job if job["num_tx"] == 1 and job["merkleProof"] == [payment_id] else None

    fields = wait("Teranode two-transaction job including the common payment", prepared, seconds=30)
    coinbase = bytes.fromhex(fields["coinbase"])

    def submit(nonce):
        result = rpc(node, "submitminingsolution", {"id": fields["id"], "nonce": nonce,
                     "time": fields["time"], "version": fields["version"], "coinbase": coinbase.hex()})
        require(result is True, "Teranode did not acknowledge the mining solution")
        return result

    yield checked_job(label, "teranode", fields["id"], parent, fields["version"], fields["time"],
                      int(fields["nBits"], 16), coinbase, payment, submit)


@contextmanager
def stratum_job(parent, payment, port, status_port, label="witnessed", *, submit_wire=None):
    """Keep the subscribed worker connection alive through optional producer alignment."""
    status = json.loads(request(f"http://127.0.0.1:{status_port}/status"))
    require(status["network"] == "regtest" and status["mining"]["tip_hash"] == parent
            and status["mining"]["observation_window_ready"], "Scary Node is not ready on this regtest parent")
    worker = "race-worker"
    with socket.create_connection(("127.0.0.1", port), timeout=30) as sock, sock.makefile("rwb") as stream:
        def send(request_id, method, args):
            stream.write(json.dumps({"id": request_id, "method": method, "params": args}).encode() + b"\n")
            stream.flush()

        def receive():
            line = stream.readline(8193)
            require(line.endswith(b"\n") and len(line) <= 8192, "incomplete Stratum response")
            message = json.loads(line)
            require(not message.get("error"), f"Stratum request failed: {message.get('error')}")
            return message

        send(1, "mining.subscribe", ["scary-node-regtest-race"])
        send(2, "mining.authorize", [worker, "regtest"])
        subscription, notify, authorized = None, None, False
        for _ in range(64):
            message = receive()
            if message.get("method") == "mining.notify":
                notify = message["params"]
            elif message.get("id") == 1:
                subscription = message["result"]
            elif message.get("id") == 2:
                authorized = message["result"] is True
                require(authorized, "worker authorization failed")
            if authorized and subscription is not None and notify is not None:
                break
        require(authorized and subscription is not None and notify is not None, "no complete Stratum job")
        job_id, words, prefix, suffix, branches, version, bits, ntime, _ = notify
        encoded = bytes.fromhex(words)
        previous = b"".join(encoded[i:i+4][::-1] for i in range(0, len(encoded), 4))
        require(previous[::-1].hex() == parent and branches == [dsha(payment).hex()],
                "Stratum job disagrees with parent or common payment")
        _, extra1, size = subscription
        require(1 <= size <= 16 and len(bytes.fromhex(extra1)) <= 16, "unexpected extranonce size")
        extra2 = bytes(size).hex()
        coinbase = bytes.fromhex(prefix + extra1 + extra2 + suffix)

        def submit(nonce):
            params = [worker, job_id, extra2, ntime, f"{nonce:08x}"]
            if submit_wire is None:
                send(3, "mining.submit", params)
            else:
                request = json.dumps({"id": 3, "method": "mining.submit", "params": params}).encode() + b"\n"
                submit_wire(stream, request)
            for _ in range(64):
                response = receive()
                if response.get("id") == 3:
                    require(response["result"] is True, "Stratum did not acknowledge the mining solution")
                    return True
            raise RuntimeError("no Stratum submission acknowledgment")

        yield checked_job(label, "stratum", job_id, parent, int(version, 16), int(ntime, 16),
                          int(bits, 16), coinbase, payment, submit)


@dataclass(frozen=True)
class RpcObservation:
    result: object
    error: object
    proxy_time: str
    proxy_request_time: str

    def receipt(self):
        endpoint = Decimal(self.proxy_time)
        elapsed = Decimal(self.proxy_request_time)
        require(endpoint.is_finite() and elapsed.is_finite() and endpoint >= elapsed >= 0,
                "invalid neutral proxy timing headers")
        row = {"ok": self.error is None, "result": self.result, "proxy_time": self.proxy_time,
               "proxy_request_time": self.proxy_request_time, "ingress_ms": int((endpoint - elapsed) * 1000)}
        if self.error is not None:
            row["error"] = self.error
        return row


def proxy_rpc(port, method, params):
    """Capture raw NGINX timing headers even when the regtest arbiter returns an RPC error."""
    require(method in ("getblocktemplate", "submitblock"), "unexpected arbiter work RPC")
    if method == "getblocktemplate":
        require(len(params) == 1 and params[0].get("mode") == "proposal", "proposal preflight required")
    body = json.dumps({"jsonrpc": "1.0", "id": 1, "method": method, "params": params}).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("POST", "/", body=body, headers={"Content-Type": "application/json", "Authorization": AUTH})
        response = conn.getresponse()
        proxy_time = response.getheader("X-Race-Proxy-Time")
        request_time = response.getheader("X-Race-Request-Time")
        require(proxy_time is not None and request_time is not None, "neutral proxy timing headers missing")
        data = response.read(65537)
        require(len(data) <= 65536, "unexpected oversized work RPC response")
        parsed = json.loads(data)
        require(parsed["id"] == 1 and "result" in parsed and "error" in parsed, "unrecognized work RPC response")
        require(200 <= response.status < 300 or parsed["error"] is not None,
                "unexpected neutral proxy HTTP status")
        return RpcObservation(parsed["result"], parsed["error"], proxy_time, request_time)
    finally:
        conn.close()


def call_receipt(callback, raw, clock_start, barrier=None):
    """RPC transport success and the RPC's own acceptance result are recorded separately."""
    if barrier is not None:
        barrier.wait(timeout=10)
    started = time.monotonic_ns() - clock_start
    try:
        result = callback(raw)
        receipt = result.receipt() if isinstance(result, RpcObservation) else {"ok": True, "result": result}
    except Exception as error:
        receipt = {"ok": False, "result": None, "error": str(error)}
    return {**receipt, "started_ns": started, "finished_ns": time.monotonic_ns() - clock_start}


def race_pair(left_job, right_job, start_nonce, relay, *, preflight, max_ticks=MAX_TICKS, swap_lanes=False, swap_dispatch=False):
    """Hash one nonce per arm per tick; submit all first-tick solutions at the neutral receiver.

    Lane offsets are 0 and 1; the nonce stride is 2. This gives distinct work even
    if two full-state miners return identical templates. Every winning body first
    goes through proposal mode on the fixed parent. Submission is immediate after
    those common preflights, with a barrier release for tied ticks.
    """
    require(1 <= max_ticks <= MAX_TICKS and 0 <= start_nonce <= 2**32 - 2 * max_ticks,
            "nonce lanes must be bounded, disjoint and fit in uint32")
    jobs = [left_job, right_job]
    require(left_job.label != right_job.label and left_job.parent == right_job.parent
            and left_job.payment == right_job.payment, "both miners require the same parent and ordinary payment")
    for job in jobs:
        require(len(job.prefix) == 76 and job.prefix[72:] == struct.pack("<I", REGTEST_BITS),
                "only regtest jobs may enter this worker")

    started = time.monotonic_ns()
    arms = [{"label": job.label, "kind": job.kind, "job_id": job.job_id,
             "header_prefix_hex": job.prefix.hex(), "coinbase_hex": job.coinbase.hex(),
             "first_nonce": start_nonce + (i ^ int(swap_lanes)), "last_nonce": None,
             "solved": False, "header_hex": None, "hash": None, "block_hex": None,
             "proposal": None, "relay": None} for i, job in enumerate(jobs)]
    solutions = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for tick in range(max_ticks):
            headers = []
            for arm, job in zip(arms, jobs):
                arm["last_nonce"] = arm["first_nonce"] + 2 * tick
                headers.append(job.header(arm["last_nonce"]))
            for i, digest in enumerate(pool.map(dsha, headers)):
                if int.from_bytes(digest, "little") <= REGTEST_TARGET:
                    raw = headers[i] + b"\x02" + jobs[i].coinbase + jobs[i].payment
                    arms[i].update(solved=True, header_hex=headers[i].hex(), hash=digest[::-1].hex(), block_hex=raw.hex())
                    solutions.append((i, raw))
            if solutions:
                break

        # Running both proposals before either submit prevents the first
        # acceptance from changing the parent tested for the other proposal.
        dispatch_order = solutions[::-1] if swap_dispatch else solutions
        for i, raw in dispatch_order:
            arms[i]["proposal"] = call_receipt(preflight, raw, started)
        if solutions and all(arms[i]["proposal"]["ok"] and arms[i]["proposal"]["result"] is None for i, _ in solutions):
            barrier = threading.Barrier(len(solutions))
            futures = [(i, pool.submit(call_receipt, relay, raw, started, barrier)) for i, raw in dispatch_order]
            for i, future in futures:
                arms[i]["relay"] = future.result()

    return {"parent": left_job.parent, "payment_txid": txid(left_job.payment), "target_bits": "207fffff",
            "start_nonce": start_nonce, "nonce_stride": 2, "swap_lanes": swap_lanes, "swap_dispatch": swap_dispatch, "max_ticks": max_ticks,
            "attempts_per_arm": tick + 1, "dispatch_order": [arms[i]["label"] for i, _ in dispatch_order],
            "arms": arms, "elapsed_ns": time.monotonic_ns() - started}
