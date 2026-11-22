import json
import unittest
from pathlib import Path

from checkpoint_upgrade import trace_interval, verify


class CheckpointEventTests(unittest.TestCase):
    def test_published_receipt_requires_recorded_build_and_peerless_restore(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "docs/evidence/chronicle-checkpoint-upgrade.json"
        )
        receipt = json.loads(path.read_text())
        self.assertEqual(verify(receipt)["original_height"], 580)
        for field, value in [
            ("image", "unverified-build"),
            ("reopened", {**receipt["reopened"], "connected_data_peers": 1}),
            ("stock_successor_header", receipt["selected_wire"][:160]),
        ]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify({**receipt, field: value})

    def test_only_the_replaced_and_closed_file_counts(self):
        original = [
            {"event": "ready", "name": "", "elapsed_ns": 0},
            {"event": "create", "name": "witness.next", "elapsed_ns": 20},
            {"event": "rename", "name": "witness.state", "elapsed_ns": 85},
            {"event": "close_write", "name": "witness.state", "elapsed_ns": 90},
        ]
        self.assertEqual(trace_interval(original, 1)["create_to_close_ns"], 70)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            trace_interval(original[:-1], 1)
        original[-1]["name"] = "witness.next"
        with self.assertRaisesRegex(ValueError, "incomplete"):
            trace_interval(original, 1)
