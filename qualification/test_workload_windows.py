import unittest
from pathlib import Path

from workload_windows import summarize


class WorkloadWindowTests(unittest.TestCase):
    def test_interruption_preserves_the_unobserved_tail_and_omits_the_rate(self):
        data = Path(__file__).resolve().parents[1] / "docs/evidence"
        summary = summarize(data / "chronicle-sustained-endorsed-interruption.json.gz")
        self.assertEqual(
            [x["unobserved_slots"] for x in summary["windows"]], [0, 0, 0, 0, 4, 50]
        )
        self.assertEqual(
            summary["windows"][0]["worker_return_matched_nonempty_scary_blocks"], 8
        )
        self.assertIsNone(summary["confirmed_payments_per_second"])
        self.assertIsNone(summary["two_successors_after_final_payment"])
        self.assertIs(summary["registered_regular_origin"], False)

    def test_original_inventory_control_has_an_observed_zero_in_every_window(self):
        data = Path(__file__).resolve().parents[1] / "docs/evidence"
        summary = summarize(data / "chronicle-sustained-inventory.json.gz")
        self.assertEqual(
            [
                x["worker_return_matched_payments_in_scary_blocks"]
                for x in summary["windows"]
            ],
            [0] * 6,
        )
        self.assertEqual(summary["recorded_primary_slots"], 300)
        self.assertEqual(summary["distinct_audited_payments"], 5_529_600)
        self.assertGreater(summary["confirmed_payments_per_second"], 3000)
        self.assertIsNone(summary["registered_joint_target"])
