"""Supply the finite regtest wallet corpus over native inventory, request and transaction messages."""

import array
import contextlib
import hashlib
import json
import mmap
import queue
import struct
import threading
import time

from sustained_corpus import BATCH, COUNT
from sustained_profiles import PROJECTS, WITNESSED_ARMS, batch_order, profile
from sustained_wallet import Wallet, wait_until

TX = struct.pack("<I", 1)


def compact(value):
    if value < 253:
        return bytes([value])
    if value <= 65535:
        return b"\xfd" + struct.pack("<H", value)
    return b"\xfe" + struct.pack("<I", value)


def requests(data):
    if not data:
        raise ValueError("empty inventory vector")
    count = data[0]
    offset = 1
    if count >= 253:
        width = (2, 4, 8)[count - 253]
        offset += width
        count = int.from_bytes(data[1:offset], "little")
    if count > 50000 or len(data) != offset + 36 * count:
        raise ValueError("invalid inventory vector")
    return (data[i : i + 36] for i in range(offset, len(data), 36))


def emit(log, row):
    log.write(json.dumps(row, separators=(",", ":")) + "\n")
    log.flush()


class Inventory:
    def __init__(self, corpus, stack):
        def mapping(name):
            f = stack.enter_context((corpus / name).open("rb"))
            return stack.enter_context(
                mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            )

        self.ids = mapping("ids.bin")
        self.frames = mapping("frames.bin")
        self.positions = array.array("Q", [0])
        offsets = json.loads((corpus / "offsets.json").read_text())
        if len(self.ids) != 32 * COUNT or len(offsets) != COUNT // BATCH + 1:
            raise ValueError("incomplete registered inventory corpus")
        pos = 0
        for end in offsets[1:]:
            for _ in range(BATCH):
                pos += 24 + struct.unpack_from("<I", self.frames, pos + 16)[0]
                self.positions.append(pos)
            if end != pos:
                raise ValueError("inventory transaction frame offsets differ")
        if pos != len(self.frames):
            raise ValueError("inventory corpus has trailing bytes")
        self.advertised = {}
        self.requested = bytearray(COUNT)
        self.served = bytearray(COUNT)
        self.total = {
            "requests": 0,
            "served": 0,
            "unknown_requests": 0,
            "unique_requested": 0,
            "unique_served": 0,
            "served_wire_bytes": 0,
        }
        self.lock = threading.Lock()

    def advertise(self, peer, ordinals):
        body = bytearray(compact(len(ordinals)))
        for ordinal in ordinals:
            digest = self.ids[ordinal * 32 : (ordinal + 1) * 32]
            self.advertised[digest] = ordinal
            body.extend(TX)
            body.extend(digest)
        peer.control(b"inv", body)

    def serve(self, peer, data):
        missing = []
        selected = []
        for item in requests(data):
            ordinal = self.advertised.get(item[4:]) if item[:4] == TX else None
            if ordinal is None:
                missing.append(item)
            else:
                selected.append(ordinal)
        with self.lock:
            self.total["requests"] += len(selected)
            self.total["unknown_requests"] += len(missing)
            for i in selected:
                if not self.requested[i]:
                    self.requested[i] = 1
                    self.total["unique_requested"] += 1
        for start in range(0, len(selected), 512):
            batch = selected[start : start + 512]
            raw = bytearray()
            for i in batch:
                raw.extend(self.frames[self.positions[i] : self.positions[i + 1]])
            peer.send(raw)
            with self.lock:
                self.total["served"] += len(batch)
                self.total["served_wire_bytes"] += len(raw)
                for i in batch:
                    if not self.served[i]:
                        self.served[i] = 1
                        self.total["unique_served"] += 1
        if missing:
            peer.control(b"notfound", compact(len(missing)) + b"".join(missing))

    def snapshot(self):
        with self.lock:
            return dict(self.total)


def produce_inventory(corpus, directory, start_value, ready, stop, retries):
    wallets = []
    sessions = []
    with contextlib.ExitStack() as stack:
        source = Inventory(corpus, stack)
        arm = profile()
        port = 19444 + PROJECTS[arm][1]

        def connect(i):
            wallet = Wallet(i, port=port, on_getdata=source.serve)
            sessions.append(wallet)
            return wallet

        try:
            wallets = [connect(i) for i in range(4)]
            ready.set()
            while not start_value.value and not stop.is_set():
                time.sleep(0.05)
            start = start_value.value
            logged_s = -1
            with (
                (directory / "producer.jsonl").open("x", buffering=1) as primary,
                (directory / "retries.jsonl").open("x", buffering=1) as drain,
                (directory / "inventory-progress.jsonl").open(
                    "x", buffering=1
                ) as progress,
            ):

                def sample(final=False):
                    nonlocal logged_s
                    elapsed = time.monotonic_ns() - start
                    sec = elapsed // 1_000_000_000
                    if final or sec > logged_s:
                        emit(progress, {"elapsed_ns": elapsed, **source.snapshot()})
                        logged_s = sec

                def offer(batch, ordinals, phase, due, position=None):
                    i = batch % 4
                    begin = time.monotonic_ns() - start
                    try:
                        if wallets[i].error:
                            wallets[i].close()
                            wallets[i] = connect(i)
                        source.advertise(wallets[i], ordinals)
                        result = "sent"
                    except (OSError, RuntimeError):
                        result = "error"
                    emit(
                        primary if phase == "offer" else drain,
                        {
                            "phase": phase,
                            **({"position": position} if arm in WITNESSED_ARMS else {}),
                            "batch": batch,
                            "count": len(ordinals),
                            "sender": i,
                            "scheduled_ns": due,
                            "started_ns": begin,
                            "finished_ns": time.monotonic_ns() - start,
                            "outcome": result,
                        },
                    )
                    sample()

                for position, batch in enumerate(batch_order(arm)):
                    due = position * 1_000_000_000 // 6
                    wait_until(start + due, stop)
                    if stop.is_set():
                        break
                    offer(
                        batch,
                        range(batch * BATCH, (batch + 1) * BATCH),
                        "offer",
                        due,
                        position,
                    )
                while not stop.is_set():
                    sample()
                    try:
                        batch, bits, due = retries.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    offer(
                        batch,
                        [batch * BATCH + i for i, flag in enumerate(bits) if not flag],
                        "rebroadcast",
                        due,
                    )
                for peer in sessions:
                    peer.close()
                    peer.reader.join(2)
                sample(final=True)
                # The hashed bit vectors commit to membership in ordered corpus-ID space.
                for name in ("requested", "served"):
                    (directory / (name + ".bits")).write_bytes(getattr(source, name))
                summary = source.snapshot()
                for name in ("requested", "served"):
                    summary[name + "_bits_sha256"] = hashlib.sha256(
                        getattr(source, name)
                    ).hexdigest()
                (directory / "inventory-final.json").write_text(
                    json.dumps(summary) + "\n"
                )
        finally:
            for peer in sessions:
                peer.close()
            (directory / "wallet-outcomes.json").write_text(
                json.dumps(
                    [
                        {
                            "sender": w.index,
                            "rejects": w.rejections,
                            "connection_error": w.error,
                        }
                        for w in sessions
                    ]
                )
                + "\n"
            )
            ready.set()


def request_reannouncement(seen, start, stop, channel):
    pace = time.monotonic_ns()
    for batch in range(COUNT // BATCH):
        if stop.is_set():
            return
        bits = bytes(seen[batch * BATCH : (batch + 1) * BATCH])
        missing = bits.count(0)
        if not missing:
            continue
        wait_until(pace, stop)
        while not stop.is_set():
            try:
                channel.put((batch, bits, pace - start), timeout=0.25)
                break
            except queue.Full:
                continue
        pace = max(pace, time.monotonic_ns()) + missing * 1_000_000_000 // 3072
