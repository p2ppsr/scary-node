"""Offline checks of native inventory and body delivery accounting for the finite signed workload."""

import contextlib
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import sustained_inventory as inventory
from sustained_corpus import MAGIC


class Peer:
    def __init__(self):
        self.controls = []
        self.bodies = []

    def control(self, name, body):
        self.controls.append((name, bytes(body)))

    def send(self, wire):
        self.bodies.append(bytes(wire))


def digest(raw):
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()


class InventoryTest(unittest.TestCase):
    def test_native_inventory_and_requested_frames(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            d = Path(tmp)
            raws = [b"receipt frame " + bytes([i]) for i in range(4)]
            ids = [digest(raw) for raw in raws]
            frames = [
                MAGIC
                + b"tx".ljust(12, b"\0")
                + struct.pack("<I", len(raw))
                + h[:4]
                + raw
                for h, raw in zip(ids, raws, strict=True)
            ]
            (d / "ids.bin").write_bytes(b"".join(ids))
            (d / "frames.bin").write_bytes(b"".join(frames))
            lengths = list(map(len, frames))
            (d / "offsets.json").write_text(
                json.dumps([0, sum(lengths[:2]), sum(lengths)])
            )
            stack.enter_context(patch.object(inventory, "COUNT", 4))
            stack.enter_context(patch.object(inventory, "BATCH", 2))
            source = inventory.Inventory(d, stack)
            peer = Peer()
            source.advertise(peer, [1, 3])
            self.assertEqual(
                peer.controls[0],
                (b"inv", b"\x02" + inventory.TX + ids[1] + inventory.TX + ids[3]),
            )
            ask = b"\x04" + b"".join(inventory.TX + ids[i] for i in [3, 1, 0, 1])
            source.serve(peer, ask)
            self.assertEqual(peer.bodies, [frames[3] + frames[1] + frames[1]])
            self.assertEqual(
                peer.controls[-1], (b"notfound", b"\x01" + inventory.TX + ids[0])
            )
            self.assertEqual(source.requested, bytes([0, 1, 0, 1]))
            self.assertEqual(source.requested, source.served)
            self.assertEqual(
                source.snapshot(),
                {
                    "requests": 3,
                    "served": 3,
                    "unknown_requests": 1,
                    "unique_requested": 2,
                    "unique_served": 2,
                    "served_wire_bytes": len(peer.bodies[0]),
                },
            )

    def test_wallet_vectors_use_compactsize(self):
        items = [inventory.TX + bytes([i % 256]) * 32 for i in range(512)]
        raw = inventory.compact(len(items)) + b"".join(items)
        self.assertEqual(list(inventory.requests(raw)), items)


if __name__ == "__main__":
    unittest.main()
