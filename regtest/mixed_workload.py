"""Originate a larger witnessed Stratum block and read back each native receiver independently."""

import hashlib
import http.client
import json
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from prove import (
    NODES,
    PORT_SHIFT,
    assert_tip,
    block,
    compose,
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
    return signed_outputs(wallet, source, 0, value, count, dest), amount


def signed_outputs(wallet, source, index, value, count, dest):
    prev = bytes.fromhex(source)[::-1] + struct.pack("<I", index)
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
    return (
        version
        + b"\x01"
        + prev
        + compact(len(sig))
        + sig
        + seq
        + compact(count)
        + dest
        + bytes(4)
    )


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


def prepare(wallet, source, value, count):
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

    return fid, amount, fund, parent


def run(wallet, source, value, count):
    fid, amount, _, parent = prepare(wallet, source, value, count)

    def child(i):
        return pay([wallet], [Coin(fid, i, amount, 0)], [(0, amount - 1000)]).raw

    with ThreadPoolExecutor(4) as pool:
        payments = sorted(pool.map(child, range(count)), key=dsha)
    result = mine_round(parent, payments, 1000 * count)
    result["funding"] = fid
    return record_ancestry(NODES[1], result)


def mine_round(parent, payments, fees):
    a, b = NODES
    count = len(payments)
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
    return {
        "parent": parent,
        "height": height,
        "solved_hash": h,
        "transactions": count,
        "fee_satoshis": fees,
        "stratum_job": job,
        "body_hex": body.hex(),
        "wire_bytes": len(body),
        "admission_ns": admitted - start,
        "stratum_fetch_solve_submit_ns": solved - admitted,
        "receivers": receivers,
        "settlement": settlement,
    }


def stream(wallet, source, value, history, rounds=8, ordinary=1024):
    """Fixed-cadence historical spends go to the full miners during witnessed Stratum assembly."""
    a, b = NODES
    per_round = ordinary + 1
    fid, amount, fund, parent = prepare(wallet, source, value, rounds * per_round)

    def make_round(r):
        offset = r * per_round

        def payment(i):
            return pay(
                [wallet], [Coin(fid, offset + i, amount, 0)], [(0, amount - 1000)]
            ).raw

        with ThreadPoolExecutor(4) as pool:
            raw = list(pool.map(payment, range(ordinary)))
        data = bytes([r + 1]) * (2 << 20)
        locking = b"\x00\x6a\x4e" + struct.pack("<I", len(data)) + data
        dest = struct.pack("<Q", 0) + compact(len(locking)) + locking
        raw.append(signed_outputs(wallet, fid, offset + ordinary, amount, 1, dest))
        return sorted(raw, key=dsha)

    prepared = [make_round(r) for r in range(rounds)]
    background = [wallet.spend(coin, val, 1000) for coin, val in history]
    start = time.monotonic_ns()

    def transact():
        offers = []
        for i, raw in enumerate(background):
            at = start + i * 2_000_000_000
            time.sleep(max(0, at - time.monotonic_ns()) / 1e9)
            opened = time.monotonic_ns()
            h = txid(raw)
            assert rpc(b, "sendrawtransaction", raw.hex()) == h
            assert mixed_sv("sendrawtransaction", raw.hex()) == h
            offers.append(
                {
                    "txid": h,
                    "input": history[i][0],
                    "open_ns": opened - start,
                    "ack_ns": time.monotonic_ns() - start,
                }
            )
        return offers

    rows = []
    with ThreadPoolExecutor(1) as senders:
        future = senders.submit(transact)
        for i, raw in enumerate(prepared):
            before = time.monotonic_ns()
            result = mine_round(parent, raw, 1000 * ordinary + amount)
            result.update(
                round=i + 1,
                start_ns=before - start,
                settled_ns=time.monotonic_ns() - start,
                parent_header_hex=block(b, parent)[0][:80].hex(),
                successors_hex=[
                    block(b, h)[0].hex() for h in result["settlement"]["successors"]
                ],
            )
            rows.append(result)
            parent = result["settlement"]["settlement_tip"]
            wait(
                "entrant reached settled tip",
                lambda: status()["mining"]["tip_hash"] == parent,
            )
            print(
                f"Mixed streaming round {i + 1}/{rounds} settled at height {result['height'] + 2}.",
                file=sys.stderr,
                flush=True,
            )
        offers = future.result()
    included = {}
    for r in rows:
        for active in r["settlement"]["successors"]:
            for tx in block(b, active)[1][1:]:
                included[txid(tx)] = active
    final = []
    remaining = sum(x["txid"] not in included for x in offers)
    if remaining:
        wait(
            "all acknowledged background spends reached block assembly",
            lambda: rpc(b, "getminingcandidate")["num_tx"] >= remaining,
        )
        last = rpc(b, "generate", 1)[0]
        wait("background settlement", lambda: assert_tip(a, last))
        wire, txs, _ = block(b, last)
        assert (
            not mixed_sv("submitblock", wire.hex())
            and mixed_sv("getbestblockhash") == last
        )
        final.append(wire.hex())
        included.update((txid(t), last) for t in txs[1:])
    for item in offers:
        item["active_block"] = included[item["txid"]]
    return {
        "release_binary_sha256": compose(
            "exec",
            "-T",
            "scary-node",
            "sha256sum",
            "/usr/local/bin/scary-node",
            capture=True,
        ).split()[0],
        "rounds": rows,
        "funding": fid,
        "funding_value": amount,
        "funding_block": fund,
        "funding_block_hex": block(b, fund)[0].hex(),
        "background": offers,
        "final_bodies_hex": final,
        "total_wall_ns": time.monotonic_ns() - start,
        "background_cadence_ns": 2_000_000_000,
        "ordinary_payments_per_round": ordinary,
        "data_bytes_per_round": 2 << 20,
    }
