import copy
import gzip
import json
import unittest
from pathlib import Path

from interrupted_workload import verify
from recover_sustained_chain import confirmed_boundary


class InterruptedWorkloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with gzip.open(
            Path(__file__).resolve().parents[1]
            / "docs/evidence/chronicle-sustained-endorsed-interruption.json.gz",
            "rt",
        ) as f:
            cls.record = json.load(f)

    def test_unobserved_slots_and_complete_window_misses(self):
        result = verify(self.record)
        self.assertEqual(result["recorded_opportunities"], 246)
        self.assertEqual(result["opportunities_without_outcomes"], 54)
        self.assertEqual(
            [w["slots_recorded"] for w in result["observed_windows"]],
            [50, 50, 50, 50, 46, 0],
        )
        self.assertTrue(result["per_window_criterion_already_missed"])
        tail = result["observed_empty_pool_suffix"]
        self.assertEqual(tail["empty_status_replies"], 1149)
        self.assertEqual(tail["unavailable_status_replies"], 0)
        self.assertEqual(tail["minimum_connected_peers"], 3)
        self.assertEqual(tail["last_populated_status"]["pending"], 19168)
        self.assertEqual(tail["first_empty_status"]["height"], 494)
        self.assertEqual(
            tail["intake_change_since_last_populated"]["sv"]["admitted"], 0
        )
        self.assertEqual(
            confirmed_boundary(self.record)["final_hash"],
            self.record["blocks"][-1]["hash"],
        )

    def test_changed_original_line(self):
        bad = copy.deepcopy(self.record)
        bad["slots"][0]["ready"] = False
        with self.assertRaisesRegex(ValueError, "original source log"):
            verify(bad)

    def test_late_membership_must_agree(self):
        bad = copy.deepcopy(self.record)
        bad["after_interruption"]["queue_readbacks"][1]["stock"]["pending"] -= 1
        with self.assertRaisesRegex(ValueError, "mempool readbacks"):
            verify(bad)
