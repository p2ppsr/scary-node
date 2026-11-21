"""Post-deadline native continuation and independently timed stock readbacks."""

import hashlib
import http.client
import json
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from cohort_archive import check
from cohort_estimate import decode_block, dsha
from matched_propagation import deliver, frame, receive, write
from sv_reference import RpcError

READ_TIMEOUT_SECONDS = 0.2
HASH_ATTEMPTS = 2


def observation_rpc(node, method, args):
    url, auth = node.http
    request = urllib.request.Request(
        url,
        data=json.dumps({"id": 1, "method": method, "params": args}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Basic " + auth},
    )
    try:
        with urllib.request.urlopen(request, timeout=READ_TIMEOUT_SECONDS) as response:
            message = json.load(response)
    except urllib.error.HTTPError as exc:
        message = json.loads(exc.read())
    if message["error"] is not None:
        fault = message["error"]
        raise RpcError(json.dumps({"error": fault}))
    return message["result"]


def sampled(rpc, n, method, args, tick):
    # Only an immutable by-hash lookup may be retried. The active tip is sampled once.
    limit = HASH_ATTEMPTS if method == "getblock" else 1
    ledger = []
    result = {}
    for _ in range(limit):
        start = time.monotonic_ns() - tick
        try:
            value = rpc(n, method, args)
            if method == "getblock":
                raw = bytes.fromhex(value)
                if len(raw) < 80 or dsha(raw[:80])[::-1].hex() != args[0]:
                    raise ValueError("wrong native body hash")
                result = {"body_sha256": hashlib.sha256(raw).hexdigest()}
            else:
                if not isinstance(value, str) or len(bytes.fromhex(value)) != 32:
                    raise ValueError("invalid active hash")
                result = {"active_hash": value}
            outcome = {"result": "observed"}
        except RpcError as e:
            code = json.loads(str(e))["error"]["code"]
            result = {"rpc_code": code}
            outcome = {"result": "rpc_error", "code": code}
        except (
            OSError,
            http.client.HTTPException,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            outcome = {
                "result": "transport_or_encoding_error",
                "type": type(exc).__name__,
            }
            if isinstance(exc, http.client.IncompleteRead):
                outcome["partial_bytes"] = len(exc.partial)
            result = {"readback_unavailable": True}
        ledger.append({**outcome, "query_ns": [start, time.monotonic_ns() - tick]})
        if outcome["result"] != "transport_or_encoding_error":
            break
    return {
        **result,
        "attempts": ledger,
        "query_ns": [ledger[0]["query_ns"][0], ledger[-1]["query_ns"][1]],
    }


def stock_readbacks(receivers, hashes, tick, deadline=None, rpc=observation_rpc):
    if deadline is not None:
        remaining = (deadline - time.monotonic_ns()) / 1e9
        if remaining > 0:
            time.sleep(remaining)

    def sample(n):
        active = sampled(rpc, n, "getbestblockhash", [], tick)
        return {
            "active_hash": active.get("active_hash"),
            "active_query_ns": active["query_ns"],
            "active_receipt": active,
            "bodies": {h: sampled(rpc, n, "getblock", [h, 0], tick) for h in hashes},
        }

    with ThreadPoolExecutor(2) as pool:
        return list(pool.map(sample, receivers))


def native_branch(f, row):
    """After the preselected receiver has mined two successors, sync all stocks via P2P."""
    from scary_dar import until

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


def qualify_observer(output):
    """Probe actual stored and unknown hashes without competing candidates or a scheduled race."""
    import contextlib
    from pathlib import Path

    from matched_fixture import atomic
    from matched_study import verify_attempts
    from run_cohort import session_lock
    from sv_reference import IMAGE, Reference

    path = Path(output)
    check(not path.exists(), "observer evidence path already exists")
    with session_lock(), contextlib.ExitStack() as stack:
        nodes = [
            stack.enter_context(Reference().running(peer=True, fast_rpc=True))
            for _ in range(2)
        ]
        h = nodes[0].rpc("generate", 1)[0]
        unknown = "ee" * 32
        bodies = stock_readbacks(nodes, [h, unknown], time.monotonic_ns())
        raw = bytes.fromhex(nodes[0].rpc("getblock", h, 0))
        for i, site in enumerate(bodies):
            check(
                verify_attempts(site["active_receipt"], 1) == "observed"
                and site["active_hash"] == nodes[i].rpc("getbestblockhash"),
                "unopposed active stock readback differs",
            )
            for ident, entry in site["bodies"].items():
                state = verify_attempts(entry, 2)
                if i == 0 and ident == h:
                    check(
                        state == "observed"
                        and entry["body_sha256"] == hashlib.sha256(raw).hexdigest(),
                        "stored stock bytes differ",
                    )
                else:
                    check(
                        state == "rpc_error" and entry["rpc_code"] == -5,
                        "stock did not report unknown hash",
                    )
        result = {
            "reference_image": IMAGE,
            "kind": "unopposed stock observer, resident and unknown hashes",
            "resident_hash": h,
            "resident_wire_hex": raw.hex(),
            "unknown_hash": unknown,
            "sites": bodies,
        }
        atomic(path, result)
        return result


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="Qualify bounded, unopposed readbacks on two stock regtest processes."
    )
    p.add_argument("receipt")
    r = qualify_observer(p.parse_args().receipt)
    print(
        json.dumps(
            {"stock_sites": len(r["sites"]), "resident": r["resident_hash"]}, indent=2
        )
    )
