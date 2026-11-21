"""Regression cases for separate prospective and retrospective mainnet evidence."""

import copy
import gzip
import json
import unittest
from pathlib import Path

from shadow_checkpoint import SCALE, next_bits, reconcile, target


class CheckpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        evidence = (
            Path(__file__).resolve().parents[1]
            / "docs/evidence/chronicle-shadow-journal.json.gz"
        )
        with gzip.open(evidence) as source:
            cls.snapshot = json.load(source)

    def test_reference_time_clamps_and_work(self):
        bits = 0x1D00FFFF
        work = SCALE // (target(bits) + 1)
        for spacing, forecast in [(1, 0x1C7FFF80), (600, bits), (2000, bits)]:
            with self.subTest(spacing=spacing):
                self.assertEqual(
                    next_bits([(i * spacing, i * work) for i in range(147)]), forecast
                )
        for encoded in [0, 0x01000001, 0x1D80FFFF, 0x23010000]:
            with self.subTest(invalid_target=encoded), self.assertRaises(ValueError):
                target(encoded)
        with self.assertRaises(ValueError):
            next_bits([(0, work)] * 146)

    def test_replays_independent_anchor(self):
        result = reconcile(self.snapshot)
        self.assertTrue(
            result["external_reference"]["absolute_chainwork_delta_matched"]
        )
        self.assertEqual(
            result["wire_headers_rehashed"]
            + result["external_reference"]["retrospective_headers_supplemented"],
            self.snapshot["audit"]["observed_transitions"],
        )
        self.assertTrue(result["receipt_confirmed_sv_only_failover"])

    def test_failover_must_match_its_own_restored_parent(self):
        example = copy.deepcopy(self.snapshot)
        example["failover"][0]["hash"] = "00" * 32
        self.assertFalse(reconcile(example)["receipt_confirmed_sv_only_failover"])

    def test_corrupt_reference_chainwork(self):
        example = copy.deepcopy(self.snapshot)
        ref = next(iter(example["reference"]["by_hash"].values()))
        ref["chainwork"] = "0"
        with self.assertRaisesRegex(ValueError, "work"):
            reconcile(example)

    def test_stale_signed_receipt(self):
        example = copy.deepcopy(self.snapshot)
        row = next(
            r for r in example["records"] if r.get("directives", {}).get("ready")
        )
        row["directives"]["last_peer_sync"]["unix_s"] = row["observed_at"] - 1201
        with self.assertRaisesRegex(ValueError, "signed prefix"):
            reconcile(example)


if __name__ == "__main__":
    unittest.main()
