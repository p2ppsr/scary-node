"""Originate a larger witnessed Stratum block and read back each native receiver independently."""

import hashlib
import http.client
import json
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from prove import (
    NODES,
    PORT_SHIFT,
    assert_tip,
    block,
    dsha,
    mixed_sv,
    mixed_settlement,
    rpc,
    solve,
    status,
    synchronize_mixed_parent,
    txid,
    wait,
)
from scenario import Coin, pay


def compact(n):
    if n < 253:
        return bytes([n])
    if n <= 65535:
        return b"\xfd" + struct.pack("<H", n)
    return b"\xfe" + struct.pack("<I", n)


def fanout(wallet, source, value, count):
    amount = (value - 3_000_000) // count
    assert amount > 2000
    dest = (
        struct.pack("<Q", amount) + compact(len(wallet.script)) + wallet.script
    ) * count
    prev = bytes.fromhex(source)[::-1] + bytes(4)
    seq = b"\xff" * 4
    version = struct.pack("<I", 1)
    code = compact(len(wallet.script)) + wallet.script
    digest = dsha(
        version
        + dsha(prev)
        + dsha(seq)
        + prev
        + code
        + struct.pack("<Q", value)
        + seq
        + dsha(dest)
        + bytes(4)
        + struct.pack("<I", 0x41)
    )
    sig = wallet.unlocking_script(digest)
    return version + b"\x01" + prev + compact(len(sig)) + sig + seq + compact(
        count
    ) + dest + bytes(4), amount


def record_ancestry(node, receipt):
    height = receipt["height"]
    funding_block = rpc(node, "getblockhash", height - 2)
    receipt.update(
        funding_block=funding_block,
        funding_block_hex=block(node, funding_block)[0].hex(),
        parent_header_hex=block(node, receipt["parent"])[0][:80].hex(),
        successor_headers_hex=[
            block(node, h)[0][:80].hex() for h in receipt["settlement"]["successors"]
        ],
    )
    return receipt


def run(wallet, source, value, count):
    a, b = NODES
    raw, amount = fanout(wallet, source, value, count)
    fid = rpc(a, "sendrawtransaction", raw.hex())
    assert fid == txid(raw)
    wait(
        "native reference assembled fanout",
        lambda: rpc(a, "getminingcandidate")["num_tx"] == 1,
    )
    fund = rpc(a, "generate", 1)[0]
    wait(
        "complete fanout reached both other miners",
        lambda: assert_tip(b, fund) and status()["mining"]["tip_hash"] == fund,
    )
    parent = rpc(b, "generate", 1)[0]
    wait(
        "common parent",
        lambda: assert_tip(a, parent) and status()["mining"]["tip_hash"] == parent,
    )
    synchronize_mixed_parent(a, parent)

    def child(i):
        return pay([wallet], [Coin(fid, i, amount, 0)], [(0, amount - 1000)]).raw

    with ThreadPoolExecutor(4) as pool:
        payments = sorted(pool.map(child, range(count)), key=dsha)
    start = time.monotonic_ns()
    conn = http.client.HTTPConnection("127.0.0.1", 19480 + PORT_SHIFT, timeout=45)
    try:
        for raw_tx in payments:
            conn.request(
                "POST",
                "/transactions",
                raw_tx,
                {"Content-Type": "application/octet-stream"},
            )
            reply = conn.getresponse()
            data = json.load(reply)
            if reply.status != 202 or data["txid"] != txid(raw_tx):
                raise RuntimeError(f"candidate admission {reply.status} {data}")
    finally:
        conn.close()
    admitted = time.monotonic_ns()
    ids = [txid(x) for x in payments]
    job, header, cb = solve("mixed-load-worker", parent, ids)
    solved = time.monotonic_ns()
    h = txid(header)
    body = header + compact(count + 1) + cb + b"".join(payments)

    def read(receiver):
        if receiver == "sv":
            wait("SV direct native adoption", lambda: mixed_sv("getbestblockhash") == h)
            adopted = time.monotonic_ns()
            wire = bytes.fromhex(mixed_sv("getblock", h, 0))
        else:
            node = NODES[int(receiver[-1]) - 1]
            wait(receiver + " native adoption", lambda: assert_tip(node, h))
            adopted = time.monotonic_ns()
            wire = block(node, h)[0]
        if wire != body:
            raise RuntimeError("receiver wire mismatch: " + receiver)
        return {
            "receiver": receiver,
            "hash": h,
            "body_sha256": hashlib.sha256(wire).hexdigest(),
            "active_readback_ns": adopted - start,
        }

    with ThreadPoolExecutor(3) as pool:
        receivers = list(pool.map(read, ["tn1", "tn2", "sv"]))
    height = rpc(b, "getblock", h, 1)["height"]
    one = rpc(b, "generate", 1)[0]
    wait("first continuation", lambda: all(assert_tip(n, one) for n in NODES))
    settlement = {"height": height, "active_hash": h}
    mixed_settlement(b, settlement, one)
    return record_ancestry(
        b,
        {
            "funding": fid,
            "parent": parent,
            "height": height,
            "solved_hash": h,
            "transactions": count,
            "fee_satoshis": 1000 * count,
            "stratum_job": job,
            "body_hex": body.hex(),
            "wire_bytes": len(body),
            "admission_ns": admitted - start,
            "stratum_fetch_solve_submit_ns": solved - admitted,
            "receivers": receivers,
            "settlement": settlement,
        },
    )
