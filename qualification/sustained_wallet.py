"""Rate-paced lab wallets deliver ordinary signed transaction messages to the isolated stock Bitcoin SV peer."""

import hashlib
import json
import os
import socket
import struct
import threading
import time
from sustained_corpus import BATCH, MAGIC


def sha256d(raw):
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()


def wait_until(ns, stop):
    while (left := (ns - time.monotonic_ns()) / 1e9) > 0 and not stop.is_set():
        stop.wait(min(left, 0.25))


class Wallet:
    def __init__(self, index):
        self.index = index
        self.sock = socket.create_connection(("127.0.0.1", 21444), timeout=8)
        self.sock.settimeout(None)
        self.sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", 8, 0)
        )
        self.out_lock = threading.Lock()
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.rejections = {}
        self.error = None
        agent = f"/scary-sustained-regtest-wallet:{index}/".encode()
        self.control(
            b"version",
            struct.pack("<iQq", 70016, 0, int(time.time()))
            + bytes(52)
            + os.urandom(8)
            + bytes([len(agent)])
            + agent
            + struct.pack("<iB", 0, 0),
        )
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        if not self.ready.wait(10) or self.error:
            self.close()
            raise RuntimeError("isolated wallet SV handshake failed")

    def control(self, command, data):
        self.send(
            MAGIC
            + command.ljust(12, b"\0")
            + struct.pack("<I", len(data))
            + sha256d(data)[:4]
            + data
        )

    def send(self, data):
        with self.out_lock:
            self.sock.sendall(data)

    def read(self):
        try:
            with self.sock.makefile("rb") as stream:
                while not self.closed.is_set():
                    header = stream.read(24)
                    if len(header) != 24 or header[:4] != MAGIC:
                        raise ValueError("peer closed or changed network")
                    length = struct.unpack_from("<I", header, 16)[0]
                    if length > 32 << 20:
                        raise ValueError("unexpected wallet control frame")
                    data = stream.read(length)
                    if len(data) != length or sha256d(data)[:4] != header[20:]:
                        raise ValueError("incomplete wallet control frame")
                    cmd = header[4:16].rstrip(b"\0")
                    if cmd == b"version":
                        self.control(b"verack", b"")
                    elif cmd == b"verack":
                        self.ready.set()
                    elif cmd == b"ping" and len(data) == 8:
                        self.control(b"pong", data)
                    elif cmd == b"reject":
                        n = data[0]
                        code = data[n + 1]
                        size = data[n + 2]
                        reason = data[n + 3 : n + 3 + size].decode(errors="replace")
                        name = f"{code}:{reason}"
                        self.rejections[name] = self.rejections.get(name, 0) + 1
        except (OSError, ValueError, IndexError) as ex:
            if not self.closed.is_set():
                self.error = type(ex).__name__
            self.ready.set()

    def close(self):
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def produce(corpus, directory, start_value, ready, stop):
    offsets = json.loads((corpus / "offsets.json").read_text())
    wallets = []
    try:
        wallets = [Wallet(i) for i in range(4)]
        ready.set()
        while not start_value.value and not stop.is_set():
            time.sleep(0.05)
        start = start_value.value
        with (
            (directory / "producer.jsonl").open("x", buffering=1) as log,
            (corpus / "frames.bin").open("rb") as f,
        ):
            for i, (lo, hi) in enumerate(zip(offsets, offsets[1:])):
                if stop.is_set():
                    break
                data = f.read(hi - lo)
                if len(data) != hi - lo:
                    raise ValueError("truncated signed batch")
                due = i * 1_000_000_000 // 6
                wait_until(start + due, stop)
                if stop.is_set():
                    break
                begin = time.monotonic_ns() - start
                wallet = wallets[i % 4]
                try:
                    if wallet.error:
                        wallet.close()
                        wallet = wallets[i % 4] = Wallet(i % 4)
                    wallet.send(data)
                    outcome = "sent"
                except OSError:
                    outcome = "error"
                log.write(
                    json.dumps(
                        {
                            "phase": "offer",
                            "batch": i,
                            "count": BATCH,
                            "sender": i % 4,
                            "scheduled_ns": due,
                            "started_ns": begin,
                            "finished_ns": time.monotonic_ns() - start,
                            "outcome": outcome,
                        }
                    )
                    + "\n"
                )
    finally:
        for wallet in wallets:
            wallet.close()
        (directory / "wallet-outcomes.json").write_text(
            json.dumps(
                [
                    {
                        "sender": w.index,
                        "rejects": w.rejections,
                        "connection_error": w.error,
                    }
                    for w in wallets
                ]
            )
        )
        ready.set()
