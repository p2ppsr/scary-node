"""Stream a known positive regtest block to and from a loopback stock SV peer."""

import hashlib
import os
import socket
import struct
import time
from contextlib import contextmanager

MAGIC = bytes.fromhex("dab5bffa")
CHUNK = 1 << 20


def exact(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise EOFError("regtest peer closed during a message")
    return data


def send_control(sock, name, body=b""):
    checksum = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    sock.sendall(
        MAGIC + name.ljust(12, b"\0") + struct.pack("<I", len(body)) + checksum + body
    )


def read_message(stream, expected_block=None):
    header = exact(stream, 24)
    if header[:4] != MAGIC:
        raise ValueError("wrong regtest envelope")
    name = header[4:16].rstrip(b"\0")
    size = int.from_bytes(header[16:20], "little")
    if name == b"block":
        if expected_block is None or size != expected_block[0]:
            raise ValueError("unexpected stock block size")
        digest = hashlib.sha256()
        remain = size
        while remain:
            data = exact(stream, min(CHUNK, remain))
            digest.update(data)
            remain -= len(data)
        if digest.hexdigest() != expected_block[1]:
            raise ValueError("stock readback differs from the archived fixture")
        value = digest.hexdigest()
    else:
        if size > 4 << 20:
            raise ValueError("oversized stock control reply")
        value = exact(stream, size)
        digest = hashlib.sha256(value)
    if hashlib.sha256(digest.digest()).digest()[:4] != header[20:24]:
        raise ValueError("stock envelope checksum differs")
    return name, value


@contextmanager
def peer(port, height, timeout=600):
    with (
        socket.create_connection(("127.0.0.1", port), timeout=10) as sock,
        sock.makefile("rb") as stream,
    ):
        sock.settimeout(timeout)
        agent = b"/scary-cadence-regtest/"
        version = (
            struct.pack("<iQq", 70016, 1, int(time.time()))
            + bytes(52)
            + os.urandom(8)
            + bytes([len(agent)])
            + agent
            + struct.pack("<i?", height, False)
        )
        send_control(sock, b"version", version)
        saw_version = False
        while True:
            name, data = read_message(stream)
            if name == b"version":
                if saw_version or len(data) < 80:
                    raise ValueError("bad stock version handshake")
                saw_version = True
                send_control(sock, b"verack")
            elif name == b"verack":
                if not saw_version or data:
                    raise ValueError("bad stock verack")
                break
        yield sock, stream


def offer(path, digest, port, height, stamp):
    size = path.stat().st_size
    if size >= 0xFFFFFFFF:
        raise ValueError("cadence fixture requires standard BSV block framing")
    with path.open("rb") as source, peer(port, height) as (sock, stream):
        header = exact(source, 80)
        source.seek(0)
        block_id = hashlib.sha256(hashlib.sha256(header).digest()).digest()
        inventory = b"\x01" + struct.pack("<I", 2) + block_id
        deadline = time.monotonic() + 600
        sent = False
        stamp("ready_to_announce")
        send_control(sock, b"inv", inventory)
        while True:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                raise TimeoutError("stock native body handoff")
            name, data = read_message(stream)
            if name == b"ping":
                send_control(sock, b"pong", data)
            elif name == b"getheaders":
                send_control(sock, b"headers", b"\x01" + header + b"\x00")
            elif name == b"getdata" and data == inventory:
                source.seek(0)
                stamp("stock_requested")
                checksum = hashlib.sha256(bytes.fromhex(digest)).digest()[:4]
                sock.sendall(
                    MAGIC
                    + b"block".ljust(12, b"\0")
                    + struct.pack("<I", size)
                    + checksum
                )
                while chunk := source.read(CHUNK):
                    sock.sendall(chunk)
                send_control(sock, b"ping", b"cadence1")
                sent = True
                stamp("body_written")
            elif sent and name == b"pong" and data == b"cadence1":
                stamp("stock_pong")
                return
            elif name == b"reject":
                raise ValueError("stock peer rejected the announcement or block")


def read_stock(port, height, block_hash, size, digest):
    with peer(port, height) as (sock, stream):
        request = b"\x01" + struct.pack("<I", 2) + bytes.fromhex(block_hash)[::-1]
        send_control(sock, b"getdata", request)
        deadline = time.monotonic() + 600
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("stock readback deadline")
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            name, data = read_message(stream, (size, digest))
            if name == b"block":
                return data
            if name == b"notfound":
                raise ValueError("stock peer did not have selected block")
            if name == b"ping":
                send_control(sock, b"pong", data)
