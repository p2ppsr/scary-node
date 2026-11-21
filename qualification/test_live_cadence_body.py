"""Offline regressions for the streaming one-block fixture using small deterministic payments."""

import hashlib
import io
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import live_cadence_body as m


def tree(digests):
    while len(digests) > 1:
        if len(digests) % 2:
            digests += digests[-1:]
        digests = [m.dsha(a + b) for a, b in zip(digests[::2], digests[1::2])]
    return digests[0]


class LiveCadenceBodyTests(unittest.TestCase):
    def test_both_merkle_implementations_on_odd_and_power_of_two_widths(self):
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            leaves = d / "leaves"
            hashes = []
            for n in range(1, 70):
                hashes.append(m.dsha(n.to_bytes(4, "little")))
                carry = m.Merkle()
                for x in hashes:
                    carry.add(x)
                leaves.write_bytes(b"".join(hashes))
                self.assertEqual(carry.root(), tree(hashes.copy()))
                self.assertEqual(carry.root(), m.disk_root(leaves, d))
            with patch.object(m, "SORT_RUN", 5):
                self.assertEqual(m.require_distinct(leaves, d), len(hashes))
                with leaves.open("ab") as f:
                    f.write(hashes[0])
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    m.require_distinct(leaves, d)

    def mini(self, d):
        parent = m.pow_header(bytes(80), m.dsha(b"parent"))
        scripts = [b"\x01" + bytes([i]) + b"\xac" for i in range(8)]
        rails = [(m.dsha(bytes([i])) + bytes(4), 5000, scripts[i]) for i in range(8)]
        active = rails.copy()
        wires = []
        ids = []
        for i in range(19):
            slot = i % 8
            point, value, _ = active[slot]
            locking = scripts[(i + i // 8 + 1) % 8]
            raw = (
                struct.pack("<I", 1)
                + b"\x01"
                + point
                + b"\x03\x02\x01\x41"
                + b"\xff" * 4
                + b"\x01"
                + struct.pack("<Q", value - m.FEE)
                + bytes([len(locking)])
                + locking
                + bytes(4)
            )
            h = m.dsha(raw)
            wires.append(raw)
            ids.append(h)
            active[slot] = (h + bytes(4), value - m.FEE, locking)
        (d / "ids.bin").write_bytes(b"".join(ids))
        frames = b"".join(
            m.MAGIC + m.TX_COMMAND + struct.pack("<I", len(r)) + h[:4] + r
            for r, h in zip(wires, ids)
        )
        (d / "frames.bin").write_bytes(frames)
        return parent, scripts, rails, wires, ids

    def test_exact_wire_prefix_fees_and_safe_fresh_output(self):
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            parent, scripts, rails, wires, ids = self.mini(d)
            ordered = hashlib.sha256(b"".join(ids)).hexdigest()
            with (
                patch.multiple(
                    m,
                    COUNT=len(ids),
                    RAILS=8,
                    PAYMENT_BYTES=sum(map(len, wires)),
                    FUNDING=m.dsha(parent)[::-1].hex(),
                ),
                patch.object(m, "source", return_value=ordered),
                patch.object(
                    m,
                    "funding",
                    side_effect=lambda _: (parent, rails.copy(), scripts.copy()),
                ),
            ):
                dst = d / "h485.block"
                built = m.build(d, dst)
                receipt = m.verify(d, dst)
                self.assertEqual(receipt["block_hash"], built["block_hash"])
                self.assertEqual(
                    receipt["wire_sha256"], hashlib.sha256(dst.read_bytes()).hexdigest()
                )
                self.assertEqual(receipt["payment_count"], 19)
                self.assertEqual(receipt["same_block_input_dependencies"], 11)
                self.assertEqual(receipt["coinbase_satoshis"], 625000000 + 19 * 200)
                self.assertEqual(receipt["ordered_prefix_txids_sha256"], ordered)
                with self.assertRaisesRegex(ValueError, "fresh"):
                    m.build(d, dst)
                backed = dst.read_bytes()
                for msg, wire in (
                    ("trailing", backed + b"a"),
                    ("ordered", backed[: -len(wires[-1])] + wires[0]),
                    ("Merkle|proof", backed[:36] + bytes(32) + backed[68:]),
                ):
                    dst.write_bytes(wire)
                    with self.assertRaisesRegex(ValueError, msg):
                        m.verify(d, dst)

    def test_nonminimal_counts_and_overwrite_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "noncanonical"):
            m.compact_from(io.BytesIO(b"\xfd\x09\x00"))
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "receipt.json"
            m.save_manifest(p, {"ok": 1})
            with self.assertRaises(FileExistsError):
                m.save_manifest(p, {"changed": 1})
