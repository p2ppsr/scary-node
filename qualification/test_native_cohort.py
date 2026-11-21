"""Regressions for block-body integrity, missed exposures and paired inference."""

import copy
from pathlib import Path
import unittest
from cohort_estimate import analyze, count_session, read
from relay_comparison import paired_report

EVIDENCE = Path(__file__).resolve().parents[1] / "docs/evidence"
RELAY = EVIDENCE / "chronicle-relay-comparison"


class NativeCohortTests(unittest.TestCase):
    def setUp(self):
        self.record = read(RELAY / "immediate/62001.json.gz")

    def test_corrupt_payment_under_original_header(self):
        for row in self.record["rows"]:
            for producer in row["producers"]:
                sol = producer["discovery"]
                if sol:
                    wire = bytearray.fromhex(sol["wire_hex"])
                    wire[-1] ^= 1
                    sol["wire_hex"] = wire.hex()
                    with self.assertRaisesRegex(ValueError, "Merkle"):
                        count_session(self.record)
                    return
        self.fail("fixture has no mined blocks")

    def test_attempts_and_exposures(self):
        rows = self.record["rows"]
        missed = next(r for r in rows if any(not p["ready"] for p in r["producers"]))
        total = count_session(self.record)["witnessed/full"][0]
        self.assertEqual(total["scheduled"], 16)
        self.assertLess(total["ready"], total["scheduled"])
        wrongly_dropped = copy.deepcopy(self.record)
        wrongly_dropped["rows"].pop(missed["index"])
        with self.assertRaisesRegex(ValueError, "incomplete session"):
            count_session(wrongly_dropped)
        tied = next(r for r in rows if all(p["ready"] for p in r["producers"]))
        tied["producers"][0]["hashes"] += 1
        with self.assertRaisesRegex(ValueError, "hash attempts"):
            count_session(self.record)

    def test_cohorts_and_ancestry(self):
        with self.assertRaisesRegex(ValueError, "wrong cohort"):
            analyze(EVIDENCE / "chronicle-native-pilot", "pilot", study="immediate")
        self.record["rows"][1]["parent"] = "00" * 32
        with self.assertRaisesRegex(ValueError, "prior settled parent"):
            count_session(self.record)

    def test_paired_influence_interval(self):
        report = paired_report(RELAY)
        contrast = report["immediate_minus_timer"]["witnessed/full"]
        expected = (0 / 37 - 11 / 38) - (19 / 42 - 2 / 43)
        self.assertAlmostEqual(contrast["difference_of_stale_differences"], expected)
        self.assertAlmostEqual(contrast["half_width"], 0.4910152333)


if __name__ == "__main__":
    unittest.main()
