"""Build and independently verify a file-backed h485 regtest block from the public signed corpus."""

import argparse
import contextlib
import hashlib
import heapq
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import decode_block, dsha
from verify_receipt import check_header, require
from verify_sustained_native import FUNDING, MAGIC, ORDERED_SOURCE, PAYMENTS, TX_COMMAND

COUNT = 1_800_000
PAYMENT_BYTES = 359_090_161
RAILS = 65_536
HEIGHT = 485
FEE = 200
PARENT_OF_FUNDING = "707cdde2b681d5f7e14b941cf91e81d4258443349fbaaf595638583a69eec945"
BITS = 0x207FFFFF
TARGET = 0x7FFFFF << 232
SORT_RUN = 32_768


def exact(file, n):
    data = file.read(n)
    require(len(data) == n, "truncated fixture bytes")
    return data


def compact(n):
    if n < 253:
        return bytes([n])
    if n <= 65535:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def compact_from(file):
    p = exact(file, 1)
    width = {253: 2, 254: 4, 255: 8}.get(p[0], 0)
    raw = p + exact(file, width)
    n = int.from_bytes(raw[1:] if width else raw, "little")
    require(raw == compact(n), "noncanonical transaction count")
    return n


def source(corpus):
    manifest = json.loads((corpus / "manifest.json").read_bytes())
    require(
        manifest["scheduled"] == PAYMENTS
        and manifest["rails"] == RAILS
        and manifest["offered_id_sha256"] == ORDERED_SOURCE
        and manifest["predecessor_height"] == 483
        and manifest["predecessor_hash"] == PARENT_OF_FUNDING,
        "unexpected published signed workload",
    )
    ids = corpus / "ids.bin"
    require(ids.stat().st_size == 32 * PAYMENTS, "invalid source ID lengths")
    all_ids = hashlib.sha256()
    prefix = hashlib.sha256()
    with ids.open("rb") as f:
        left = 32 * COUNT
        while left:
            data = exact(f, min(left, 1 << 20))
            prefix.update(data)
            all_ids.update(data)
            left -= len(data)
        while data := f.read(1 << 20):
            all_ids.update(data)
    require(
        all_ids.hexdigest() == ORDERED_SOURCE,
        "ordered source is not the published workload",
    )
    return prefix.hexdigest()


def funding(corpus):
    file = corpus / "funding.block"
    require(file.stat().st_size < 8 << 20, "unexpected funding block length")
    wire = file.read_bytes()
    check_header(wire[:80], FUNDING, PARENT_OF_FUNDING)
    _, txs, outputs = decode_block(wire, FUNDING)
    require(
        len(txs) == 1 + RAILS // 4
        and txs[0][41] >= 3
        and txs[0][42:45] == b"\x02\xe4\x01",
        "unexpected h484 coinbase or number of funded transactions",
    )
    rails = []
    for tx, outs in zip(txs[1:], outputs[1:]):
        require(len(outs) == 4, "expected four funded outputs per split")
        h = dsha(tx)
        rails.extend(
            (h + struct.pack("<I", i), amount, script)
            for i, (amount, script) in enumerate(outs)
        )
    scripts = [r[2] for r in rails[:8]]
    require(
        len(set(scripts)) == 8
        and all(r[2] == scripts[i % 8] for i, r in enumerate(rails)),
        "registered funded owners differ",
    )
    return wire[:80], rails, scripts


def subsidy(height):
    return 50 * 100_000_000 >> (height // 150)


def coinbase(height, reward, payout):
    # h485 is a positive, minimally encoded two-byte BIP34 height.
    h = height.to_bytes(2, "little")
    require(h[-1] != 0 and h[-1] < 128, "expected positive two-byte lab height")
    script = bytes([len(h)]) + h
    return (
        struct.pack("<I", 1)
        + b"\x01"
        + bytes(32)
        + b"\xff" * 4
        + compact(len(script))
        + script
        + b"\xff" * 4
        + b"\x01"
        + struct.pack("<Q", reward)
        + compact(len(payout))
        + payout
        + bytes(4)
    )


class Merkle:
    """A binary carry stack holds at most one internal digest per tree level."""

    def __init__(self):
        self.levels = []

    def add(self, h):
        level = 0
        while level < len(self.levels) and self.levels[level] is not None:
            h = dsha(self.levels[level] + h)
            self.levels[level] = None
            level += 1
        if level == len(self.levels):
            self.levels.append(h)
        else:
            self.levels[level] = h

    def root(self):
        h = None
        level = 0
        for i, left in enumerate(self.levels):
            if left is None:
                continue
            if h is None:
                h = left
            else:
                while level < i:
                    h = dsha(h + h)
                    level += 1
                h = dsha(left + h)
                level += 1
            level = max(level, i)
        require(h is not None, "empty Merkle tree")
        return h


def source_frames(corpus, count):
    with (
        (corpus / "frames.bin").open("rb") as frames,
        (corpus / "ids.bin").open("rb") as ids,
    ):
        for _ in range(count):
            header = exact(frames, 24)
            require(
                header[:4] == MAGIC and header[4:16] == TX_COMMAND,
                "invalid native transaction envelope",
            )
            size = struct.unpack_from("<I", header, 16)[0]
            require(0 < size <= 1024, "unexpected signed payment size")
            raw = exact(frames, size)
            digest = dsha(raw)
            require(
                digest[:4] == header[20:] and digest == exact(ids, 32),
                "native source differs from ordered IDs",
            )
            yield raw, digest


def pow_header(parent, root):
    prefix = (
        parent[:4]
        + dsha(parent)
        + root
        + struct.pack("<II", struct.unpack_from("<I", parent, 68)[0] + 600, BITS)
    )
    for nonce in range(1 << 32):
        header = prefix + struct.pack("<I", nonce)
        if int.from_bytes(dsha(header), "little") <= TARGET:
            return header
    raise RuntimeError("regtest nonce space exhausted")


def build(corpus, path):
    require(not path.exists(), "specify a fresh output block path")
    expected_prefix = source(corpus)
    parent, rails, scripts = funding(corpus)
    del rails
    cb = coinbase(HEIGHT, subsidy(HEIGHT) + COUNT * FEE, scripts[0])
    tree = Merkle()
    tree.add(dsha(cb))
    prefix = hashlib.sha256()
    total = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    # Hard-link a completely flushed file into its fresh final name. Never replace an existing body.
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".cadence-", mode="w+b"
    ) as out:
        out.write(bytes(80) + compact(COUNT + 1) + cb)
        for raw, digest in source_frames(corpus, COUNT):
            out.write(raw)
            tree.add(digest)
            prefix.update(digest)
            total += len(raw)
        require(
            total == PAYMENT_BYTES and prefix.hexdigest() == expected_prefix,
            "wrong transfer prefix or raw byte count",
        )
        header = pow_header(parent, tree.root())
        out.seek(0)
        out.write(header)
        out.flush()
        os.fsync(out.fileno())
        os.link(out.name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return {
        "block_hash": dsha(header)[::-1].hex(),
        "height": HEIGHT,
        "funding_parent": FUNDING,
        "payment_count": COUNT,
        "payment_wire_bytes": total,
        "wire_bytes": path.stat().st_size,
        "ordered_prefix_txids_sha256": expected_prefix,
    }


def one_input_output(f):
    # The independently supplied published wires have one input/output and short scripts.
    # Enforce this shape before reading variable-size fields from the candidate file.
    version = exact(f, 4)
    require(exact(f, 1) == b"\x01", "expected exactly one input")
    point = exact(f, 36)
    n = exact(f, 1)
    require(n[0] < 253, "unexpected unlocking script length")
    unlock = exact(f, n[0])
    sequence = exact(f, 4)
    require(exact(f, 1) == b"\x01", "expected exactly one output")
    value = exact(f, 8)
    m = exact(f, 1)
    require(m[0] < 253, "unexpected locking script length")
    script = exact(f, m[0])
    lock = exact(f, 4)
    raw = (
        version
        + b"\x01"
        + point
        + n
        + unlock
        + sequence
        + b"\x01"
        + value
        + m
        + script
        + lock
    )
    require(
        version == b"\x01\x00\x00\x00" and sequence == b"\xff" * 4 and lock == bytes(4),
        "unexpected transaction version, sequence or locktime",
    )
    return raw, point, unlock, int.from_bytes(value, "little"), script


def disk_root(first, tmp):
    current = first
    n = current.stat().st_size // 32
    require(n and current.stat().st_size == 32 * n, "invalid Merkle leaves")
    level = 0
    while n > 1:
        next_path = tmp / f"merkle-{level}"
        with current.open("rb") as nodes, next_path.open("wb") as parents:
            for i in range(0, n, 2):
                left = exact(nodes, 32)
                right = exact(nodes, 32) if i + 1 < n else left
                parents.write(dsha(left + right))
        if current != first:
            current.unlink()
        current = next_path
        n = (n + 1) // 2
        level += 1
    with current.open("rb") as final:
        return exact(final, 32)


def require_distinct(path, tmp):
    # External sorting bounds both memory and open files for the registered prefix.
    chunks = []
    with path.open("rb") as f:
        while buf := f.read(32 * SORT_RUN):
            require(len(buf) % 32 == 0, "truncated digest in uniqueness audit")
            ordered = sorted(buf[i : i + 32] for i in range(0, len(buf), 32))
            chunk = tmp / f"sort-{len(chunks)}"
            chunk.write_bytes(b"".join(ordered))
            chunks.append(chunk)
    with contextlib.ExitStack() as stack:
        files = [stack.enter_context(p.open("rb")) for p in chunks]
        previous = None
        total = 0
        for h in heapq.merge(*(iter(lambda f=f: f.read(32), b"") for f in files)):
            require(h != previous, "duplicate transaction ID")
            previous = h
            total += 1
    return total


def verify(corpus, block):
    expected_prefix = source(corpus)
    parent, rails, scripts = funding(corpus)
    initial = sum(r[1] for r in rails)
    pre_time = struct.unpack_from("<I", parent, 68)[0]
    ordered = hashlib.sha256()
    entire = hashlib.sha256()
    total = fees = 0
    transfers_per_rail = bytearray(RAILS)
    # Keep only one current outpoint/value per rail and one payment's wire in memory.
    with (
        tempfile.TemporaryDirectory(dir=block.parent) as temp,
        block.open("rb") as f,
        (corpus / "ids.bin").open("rb") as ids,
    ):
        tmp = Path(temp)
        h = exact(f, 80)
        ident = dsha(h)[::-1].hex()
        check_header(h, ident, FUNDING)
        require(
            h[:4] == parent[:4]
            and struct.unpack_from("<I", h, 68)[0] == pre_time + 600,
            "wrong version or ten-minute header time",
        )
        require(compact_from(f) == COUNT + 1, "wrong block transaction count")
        entire.update(h + compact(COUNT + 1))
        cb, point, unlock, claimed, payout = one_input_output(f)
        require(
            point == bytes(32) + b"\xff" * 4
            and unlock == b"\x02\xe5\x01"
            and payout == scripts[0],
            "wrong h485 coinbase",
        )
        entire.update(cb)
        leaves = tmp / "leaves"
        with leaves.open("wb") as merkle:
            merkle.write(dsha(cb))
            for i in range(COUNT):
                raw, point, sig, amount, locking = one_input_output(f)
                digest = dsha(raw)
                require(
                    digest == exact(ids, 32),
                    "block differs from the exact ordered published payment prefix",
                )
                slot = i % RAILS
                transfers_per_rail[slot] += 1
                prev, value, _ = rails[slot]
                require(
                    point == prev
                    and value - amount == FEE
                    and len(sig) >= 3
                    and sig[0] == len(sig) - 1
                    and sig[-1] == 0x41
                    and locking == scripts[(i + i // RAILS + 1) % 8],
                    "invalid rail predecessor, owner, signature form or fee",
                )
                rails[slot] = (digest + bytes(4), amount, locking)
                fees += value - amount
                ordered.update(digest)
                merkle.write(digest)
                entire.update(raw)
                total += len(raw)
        require(not f.read(1), "trailing block bytes")
        require(
            ordered.hexdigest() == expected_prefix and total == PAYMENT_BYTES,
            "wrong prefix digest or payment size",
        )
        require(disk_root(leaves, tmp) == h[36:68], "incorrect computed Merkle root")
        require(
            require_distinct(leaves, tmp) == COUNT + 1,
            "distinct transaction count differs",
        )
    final = sum(r[1] for r in rails)
    require(
        initial - final == fees == COUNT * FEE and claimed == subsidy(HEIGHT) + fees,
        "coinbase reward or transfer fee sum differs",
    )
    return {
        "height": HEIGHT,
        "funding_parent": FUNDING,
        "parent_timestamp": pre_time,
        "block_timestamp": pre_time + 600,
        "header_hex": h.hex(),
        "block_hash": ident,
        "merkle_root_internal": h[36:68].hex(),
        "funded_rails": RAILS,
        "rails_by_payment_count": {
            str(n): transfers_per_rail.count(n) for n in sorted(set(transfers_per_rail))
        },
        "payment_count": COUNT,
        "distinct_block_transactions": COUNT + 1,
        "parent_inputs_from_funding": RAILS,
        "same_block_input_dependencies": COUNT - RAILS,
        "payment_wire_bytes": total,
        "wire_bytes": block.stat().st_size,
        "ordered_prefix_txids_sha256": ordered.hexdigest(),
        "published_full_ordered_txids_sha256": ORDERED_SOURCE,
        "wire_sha256": entire.hexdigest(),
        "initial_funded_satoshis": initial,
        "final_rail_satoshis": final,
        "fees_satoshis": fees,
        "subsidy_satoshis": subsidy(HEIGHT),
        "coinbase_satoshis": claimed,
    }


def save_manifest(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w") as f:
        json.dump(result, f, indent=2, allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        os.link(f.name, path)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("build", "verify"))
    p.add_argument("corpus", type=Path)
    p.add_argument("block", type=Path)
    p.add_argument(
        "--manifest",
        type=Path,
        help="write the verified receipt to a fresh JSON file (verify only)",
    )
    a = p.parse_args()
    if a.manifest and a.action != "verify":
        p.error("--manifest requires verify")
    # Reuse the same file lock as the native studies without requiring their runtime dependencies.
    import fcntl

    with (Path(__file__).resolve().parents[1] / ".native/cohort-session.lock").open(
        "a"
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = (build if a.action == "build" else verify)(a.corpus, a.block)
        if a.manifest:
            save_manifest(a.manifest, result)
        print(json.dumps(result, indent=2))
