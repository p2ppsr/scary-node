"""Verify observed mainnet transaction bytes, earlier native funding outputs and Merkle proofs."""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import dsha, parse_tx, read_varint, txid
from shadow import check_header
from shadow_inclusion import require


def unpack_state(handle):
    # Production writes and atomically renames a complete MessagePack checkpoint.
    # This reader never acquires the node's writer lock or mutates its state.
    import msgpack

    handle.seek(0)
    data = handle.read()
    payload = memoryview(data)[8:-32]
    require(
        data[:8] == b"SCARYWS1" and hashlib.sha256(payload).digest() == data[-32:],
        "invalid committed checkpoint digest",
    )
    state = msgpack.unpackb(payload, raw=False, strict_map_key=False, use_list=False)
    require(
        state["regtest"] is False and state["headers"],
        "expected a committed mainnet tip",
    )
    state["_checkpoint_sha256"] = data[-32:].hex()
    return state


def wire_body(applied):
    raw = applied["raw"]
    return raw if isinstance(raw, bytes) else b"".join(raw)


def checked_header(raw, height):
    row = {
        "height": height,
        "header_hex": raw.hex(),
        "hash": txid(raw),
        "previous": raw[4:36][::-1].hex(),
        "bits": int.from_bytes(raw[72:76], "little"),
    }
    check_header(row)
    return row


def inputs(raw):
    count, i = read_varint(raw, 4)
    points = []
    for _ in range(count):
        require(i + 36 <= len(raw), "truncated transaction outpoint")
        points.append(
            (
                raw[i : i + 32][::-1].hex(),
                int.from_bytes(raw[i + 32 : i + 36], "little"),
            )
        )
        size, script = read_varint(raw, i + 36)
        i = script + size + 4
    _, outs, end = parse_tx(raw, 0)
    require(end == len(raw) and points and outs, "incomplete transaction")
    return points, outs


def proofs(raw, height, expected, wanted):
    header = checked_header(raw[:80], height)
    require(header["hash"] == expected, "native source returned another block")
    count, cursor = read_varint(raw, 80)
    layer, chosen, out = [], {}, {}
    for i in range(count):
        tx, _, cursor = parse_tx(raw, cursor)
        node = dsha(tx)
        layer.append(node)
        tid = node[::-1].hex()
        if tid in wanted:
            require(tid not in chosen, "ambiguous funding transaction identity")
            chosen[tid] = i
            out[tid] = {"tx": tx.hex(), "index": i, "branch": [], "count": count}
    require(count and cursor == len(raw), "incomplete native source block")
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        for tid, n in chosen.items():
            out[tid]["branch"].append(layer[n ^ 1].hex())
            chosen[tid] = n // 2
        layer = [dsha(a + b) for a, b in zip(layer[::2], layer[1::2], strict=True)]
    require(layer[0] == raw[36:68], "native body does not match its Merkle commitment")
    return {"height": height, "header": raw[:80].hex(), "transactions": out}


def verify_funding(row, expected):
    h = bytes.fromhex(row["header"])
    require(
        checked_header(h, row["height"])["hash"] == expected,
        "funding proof on another block",
    )
    found = {}
    for tid, p in row["transactions"].items():
        raw = bytes.fromhex(p["tx"])
        require(txid(raw) == tid, "funding transaction ID mismatch")
        node = dsha(raw)
        n = p["index"]
        width = p["count"]
        require(0 <= n < width, "invalid Merkle index")
        for sibling in p["branch"]:
            other = bytes.fromhex(sibling)
            require(len(other) == 32 and width > 1, "invalid Merkle path")
            if (n ^ 1) >= width:
                require(other == node, "Merkle odd tail mismatch")
            node = dsha(other + node) if n & 1 else dsha(node + other)
            n //= 2
            width = (width + 1) // 2
        require(
            n == 0 and width == 1 and node == h[36:68], "funding Merkle branch mismatch"
        )
        found[tid] = inputs(raw)[1]
    return found


def verify_fees(raws, previous, committed=None):
    created, consumed, external, ids = {}, set(), set(), set()
    fees = []
    for wire in raws:
        tx = bytes.fromhex(wire)
        tid = txid(tx)
        require(tid not in ids, "duplicate candidate transaction")
        ids.add(tid)
        points, outs = inputs(tx)
        value = 0
        for p in points:
            require(p not in consumed, "duplicate candidate spend")
            if p in created:
                v = created[p][0]
            else:
                key = f"{p[0]}:{p[1]}"
                require(key in previous, "input lacks a witnessed prevout")
                external.add(key)
                v = previous[key]["value"]
            consumed.add(p)
            value += v
        fee = value - sum(v for v, _ in outs)
        require(fee >= 0, "candidate creates money")
        fees.append(fee)
        for i, o in enumerate(outs):
            created[tid, i] = o
    require(external == previous.keys(), "unused external input evidence")
    if committed is not None:
        require(
            fees == committed, "independent per-payment fees differ from admitted fees"
        )
    return fees
