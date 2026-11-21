"""Race a real Stratum solution against the first sequence report or signed freeze bytes."""

import argparse
import concurrent.futures
import hashlib
import json
import struct
import tempfile
import threading
import time
from pathlib import Path

from native_cohort import funding_transactions
from peer_loss import nonce
from run_cohort import session_lock
from scary_dar import (
    ROOT,
    LabFeed,
    dsha,
    empty_successor,
    entrant,
    get_port,
    response,
    stratum_job,
    until,
)
from sv_reference import IMAGE, Reference, RpcError


class Ingress:
    def __init__(self, mode):
        self.mode = mode
        self.start = time.monotonic_ns()
        self.times = {}
        self.prefix = threading.Event()
        self.barrier = threading.Barrier(2, timeout=8)
        self.offsets = {}

    def mark(self, label):
        self.times[label] = time.monotonic_ns() - self.start

    def write(self, side, stream, raw, at, gated):
        self.mark(side + "_write_start")
        self.offsets[side] = {"prefix_bytes": at, "total_bytes": len(raw)}
        stream.write(raw[:at])
        stream.flush()
        self.mark(side + "_prefix_flushed")
        if gated:
            if side == "stratum":
                self.prefix.set()
            self.barrier.wait()
        self.mark(side + "_tail_start")
        # A directive can invalidate a job while its worker is still writing.
        # An observed failure is preserved separately from a completed write.
        stream.write(raw[at:])
        stream.flush()
        self.mark(side + "_tail_flushed")

    def share(self, stream, raw):
        self.write("stratum", stream, raw, len(raw) - 1, True)


class RacingFeed(LabFeed):
    gate = None

    def write_response(self, path, raw, stream):
        g = self.gate
        if not self.records or g is None:
            return super().write_response(path, raw, stream)
        side = {"/health": "sequence", "/alerts": "signed"}[path]
        if (
            json.loads(raw).get("sequence" if side == "sequence" else "latest_sequence")
            != 1
        ):
            return super().write_response(path, raw, stream)
        if side + "_write_start" in g.times:
            return super().write_response(path, raw, stream)
        if side == "signed":
            signed_hex = self.records[0]["raw"].encode()
            offset = raw.index(signed_hex) + len(signed_hex) // 2
        else:
            offset = len(raw) - 1
        g.write(side, stream, raw, offset, g.mode == side)


def readback(ref, candidate):
    best = ref.rpc("getbestblockhash")
    try:
        info = ref.rpc("getblockheader", candidate)
    except RpcError as e:
        if "Block not found" not in str(e):
            raise
        info = None
    return {
        "active": best,
        "candidate_confirmations": None if info is None else info["confirmations"],
    }


def run(mode, binary):
    with (
        session_lock(),
        tempfile.TemporaryDirectory() as temp,
        Reference().running(peer=True, fast_rpc=True) as stock,
    ):
        directory = Path(temp)
        stock.rpc("generate", 105)
        feed = RacingFeed(stock, directory)
        port, worker = get_port(), get_port()
        try:
            with entrant(binary, stock, feed, directory, port, worker) as api:

                def status():
                    return response(api + "/status")["mining"]

                funding, (payment,) = funding_transactions(stock, 1)
                funding_id = stock.rpc("sendrawtransaction", funding)
                creating = stock.rpc("generate", 1)[0]
                until(lambda: status()["tip_hash"] == creating, "funding block")
                parent = stock.rpc("generate", 1)[0]
                until(lambda: status()["tip_hash"] == parent, "candidate parent")
                payment_id = stock.rpc("sendrawtransaction", payment.hex())
                until(
                    lambda: status()["pending_transactions"] == 1,
                    "minable native payment",
                )
                height = stock.rpc("getblockcount") + 1
                payload = bytes.fromhex(funding_id) + struct.pack(
                    "<QQQB", 0, height, 2**31 - 1, 0
                )
                record = feed.prepare(2, payload)
                gate = Ingress(mode)
                feed.gate = gate
                with stratum_job(
                    parent,
                    payment,
                    worker,
                    port,
                    "concurrent-freeze",
                    submit_wire=gate.share,
                ) as job:
                    n = nonce(job)
                    header = job.header(n)
                    candidate = dsha(header)[::-1].hex()
                    raw = header + b"\x02" + job.coinbase + payment
                    proposal = stock.rpc(
                        "getblocktemplate", {"mode": "proposal", "data": raw.hex()}
                    )
                    assert proposal is None
                    before = readback(stock, candidate)
                    assert before == {"active": parent, "candidate_confirmations": None}

                    def submit():
                        try:
                            accepted = job.submit_native(n)
                        except (AssertionError, RuntimeError, OSError, ValueError) as e:
                            result = {
                                "acknowledged": False,
                                "reason": f"{type(e).__name__}: {e}",
                            }
                        else:
                            result = {"acknowledged": bool(accepted)}
                        gate.mark("worker_result")
                        return result

                    with concurrent.futures.ThreadPoolExecutor(1) as executor:
                        outcome = executor.submit(submit)
                        assert gate.prefix.wait(5)
                        gate.mark("record_available")
                        feed.records.append(record)
                        result = outcome.result(timeout=12)
                    until(
                        lambda: status()["durable_directive_sequence"] == 1,
                        "durably applied overlapping freeze",
                    )
                    gate.mark("durable_sequence_observed")
                    pre_stock_freeze = readback(stock, candidate)
                    reply = stock.rpc(
                        "addToConsensusBlacklist",
                        {
                            "funds": [
                                {
                                    "txOut": {"txId": funding_id, "vout": 0},
                                    "enforceAtHeight": [
                                        {"start": height, "stop": 2**31 - 1}
                                    ],
                                    "policyExpiresWithConsensus": False,
                                }
                            ]
                        },
                    )
                    assert not reply["notProcessed"]
                    gate.mark("stock_freeze_applied")
                    after = readback(stock, candidate)
                    next_block = empty_successor(stock)
                    next_height = stock.rpc("getblockcount")

                    def received():
                        lines = (directory / "node.log").read_text().splitlines()
                        return any(
                            line.startswith("{")
                            and (x := json.loads(line)).get("event") == "body_observed"
                            and x.get("height") == next_height
                            for line in lines
                            if line.endswith("}")
                        )

                    until(received, "independent stock successor delivered")
                    final = readback(stock, candidate)
                assert (
                    directory / "state/regtest/alerts/0000000001.signed"
                ).read_bytes().hex() == record["raw"]
                applied = json.loads(
                    (directory / "state/regtest/alerts/0000000001.receipt").read_text()
                )
                s = status()
                if final["candidate_confirmations"] is not None:
                    assert stock.rpc("getblock", candidate, 0) == raw.hex()
                return {
                    "mode": mode,
                    "reference": IMAGE,
                    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    "height": height,
                    "parent": parent,
                    "creating_wire": stock.rpc("getblock", creating, 0),
                    "parent_wire": stock.rpc("getblock", parent, 0),
                    "candidate": candidate,
                    "candidate_wire": raw.hex(),
                    "payment_txid": payment_id,
                    "funding_txid": funding_id,
                    "public_keys": feed.keys,
                    "signed_freeze": record["raw"],
                    "receipt": applied,
                    "timing_ns": gate.times,
                    "ingress_offsets": gate.offsets,
                    "worker": result,
                    "durable_sequence": s["durable_directive_sequence"],
                    "entrant_final_tip": s["tip_hash"],
                    "solved_blocks": s["solved_blocks"],
                    "solutions": s["solutions"],
                    "stock_freeze_result": reply,
                    "stock_before": before,
                    "stock_proposal_before_freeze": proposal,
                    "stock_before_local_rpc_freeze": pre_stock_freeze,
                    "stock_after_local_rpc_freeze": after,
                    "stock_final": final,
                    "stock_successor_wire": stock.rpc("getblock", next_block, 0),
                }
        finally:
            feed.server.shutdown()
            feed.server.server_close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/scary-node")
    a = p.parse_args()
    rows = []
    for mode in ("sequence", "signed"):
        rows.append(run(mode, a.binary))
        partial = a.receipt.with_suffix(a.receipt.suffix + ".partial")
        partial.write_text(json.dumps(rows, indent=2) + "\n")
    partial.replace(a.receipt)
