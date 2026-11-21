"""Post-deadline native continuation and independently timed stock readbacks."""

import hashlib
import json
import struct
import time
from concurrent.futures import ThreadPoolExecutor

from cohort_archive import check
from cohort_estimate import decode_block, dsha
from matched_propagation import deliver, frame, receive, write
from scary_dar import until
from sv_reference import RpcError


def stock_readbacks(receivers, hashes, tick, deadline=None):
    # Separate stock sockets and timing brackets for each RPC. A matched pong
    # is a transport upper bound; these calls prove storage and active selection.
    if deadline is not None:
        remaining = (deadline - time.monotonic_ns()) / 1e9
        if remaining > 0:
            time.sleep(remaining)

    def sample(n):
        start = time.monotonic_ns() - tick
        tip = n.rpc("getbestblockhash")
        result = {
            "active_hash": tip,
            "active_query_ns": [start, time.monotonic_ns() - tick],
            "bodies": {},
        }
        for h in hashes:
            start = time.monotonic_ns() - tick
            try:
                body = bytes.fromhex(n.rpc("getblock", h, 0))
                entry = {"body_sha256": hashlib.sha256(body).hexdigest()}
            except RpcError as e:
                message = json.loads(str(e))["error"]
                check(
                    message["code"] in (-1, -5), "unexpected stock readback RPC error"
                )
                entry = {"rpc_code": message["code"], "rpc_message": message["message"]}
            entry["query_ns"] = [start, time.monotonic_ns() - tick]
            result["bodies"][h] = entry
        return result

    with ThreadPoolExecutor(2) as pool:
        return list(pool.map(sample, receivers))


def native_branch(f, row):
    """After the preselected receiver has mined two successors, sync all stocks via P2P."""
    chosen = row["plan"]["successor_receiver"]
    stock = f["stocks"][2 + chosen]
    hashes = [stock.rpc("getblockhash", row["height"] + i) for i in range(3)]
    ids = [bytes.fromhex(h)[::-1] for h in hashes]
    source = f["dest"][0][chosen]
    deadline = time.monotonic_ns() + 15_000_000_000
    write(
        source,
        frame("getdata", bytes([3]) + b"".join(struct.pack("<I", 2) + h for h in ids)),
        deadline,
    )
    bodies = {}
    while len(bodies) < 3:
        name, payload = receive(source, deadline)
        if name == "block" and dsha(payload[:80]) in ids:
            ident = dsha(payload[:80])[::-1].hex()
            decode_block(payload, ident)
            bodies[ident] = payload
        if name == "ping" and len(payload) == 8:
            write(source, frame("pong", payload), deadline)
    ordered = [bodies[h] for h in hashes]
    previous = row["parent"]
    for h, raw in zip(hashes, ordered):
        check(raw[4:36][::-1].hex() == previous, "native continuation ancestry differs")
        previous = h
    headers = frame(
        "headers", bytes([3]) + b"".join(raw[:80] + b"\x00" for raw in ordered)
    )
    destinations = [*f["roots"], f["dest"][0][1 - chosen]]
    targets = [*f["stocks"][:2], f["stocks"][2 + 1 - chosen]]

    def sync(entry):
        link, target = entry
        for _ in range(2):
            write(link, headers, time.monotonic_ns() + 3_000_000_000)
            for raw in ordered:
                now = time.monotonic_ns()
                ack = deliver(link, raw, now, now, 3_000_000_000)
                check(
                    "pong_ns" in ack,
                    "native continuation stream lost its acknowledgment",
                )
            if target.rpc("getbestblockhash") == hashes[-1]:
                break
        until(
            lambda: target.rpc("getbestblockhash") == hashes[-1],
            "native continuation selected by stock",
        )
        return target.rpc("getbestblockhash")

    with ThreadPoolExecutor(3) as pool:
        tips = list(pool.map(sync, zip(destinations, targets)))
    row["native_continuation"] = {
        "source_receiver": chosen,
        "hashes": hashes,
        "body_sha256": [hashlib.sha256(raw).hexdigest() for raw in ordered],
        "other_stock_active_tips": tips,
    }
