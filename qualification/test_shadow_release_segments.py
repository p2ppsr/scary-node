"""Release boundaries and restored process events in the frozen 117-tip observation prefix."""

import copy
import json
import unittest
from pathlib import Path

from shadow import audit
from shadow_checkpoint import qualification, reconcile
from shadow_first_candidate import load
from shadow_release_segments import report, segments

EVIDENCE = Path(__file__).resolve().parents[1] / "docs/evidence"
PREFIX = EVIDENCE / "chronicle-mainnet-h976538.json.gz"


class ReleaseSegmentTests(unittest.TestCase):
    def test_published_prefix_counts_only_actual_tip_decisions(self):
        measured = report(PREFIX)
        self.assertEqual(
            measured,
            json.loads(
                (EVIDENCE / "chronicle-mainnet-release-segments-117.json").read_bytes()
            ),
        )
        self.assertFalse(measured["qualified"])
        self.assertEqual(measured["observed_tips"], 117)
        self.assertEqual(measured["unknown_executable_tips"], 97)
        self.assertEqual(measured["transaction_bearing_unsigned_templates"], 0)
        groups = measured["segments"]
        self.assertEqual([s["observed_tips"] for s in groups], [97, 2, 11, 4, 3])
        self.assertEqual(sum(s["observed_tips"] for s in groups), 117)
        self.assertEqual(len(measured["starts"]), 14)
        ending = measured["starts"][-2:]
        self.assertEqual(
            [s["preceding_tip"]["height"] for s in ending], [976535, 976535]
        )
        self.assertTrue(all(s["restores_preceding_tip"] for s in ending))
        self.assertEqual(
            [s["observed_tips_until_next_start_in_window"] for s in ending], [0, 3]
        )
        drill = measured["failover_attempts"][0]
        self.assertTrue(drill["verified_in_window"])
        self.assertEqual(
            [s["record"] for s in drill["matched_tip_and_start_records"]],
            [7, 8, 24, 28],
        )
        self.assertTrue(
            all(
                s["executable_sha256"] is None
                for s in drill["matched_tip_and_start_records"]
            )
        )

    def test_missing_restore_excludes_the_prefix_without_losing_the_preceding_release_start(
        self,
    ):
        snapshot = load(PREFIX)
        start = next(
            r
            for r in snapshot["records"]
            if (r.get("executable_sha256") or "").startswith("da070369")
        )
        start["restored_tip"] = "0" * 64
        snapshot["audit"] = audit(json.dumps(row) for row in snapshot["records"])
        checked = qualification(snapshot, reconcile(snapshot))
        measured = segments(snapshot, checked)
        self.assertEqual(measured["excluded_prefix_in_current_run"], 110)
        self.assertEqual([s["observed_tips"] for s in measured["segments"]], [4, 3])
        self.assertEqual(
            measured["segments"][0]["start_record_for_first_tip"],
            measured["starts"][0]["record"],
        )
        self.assertFalse(measured["starts"][0]["restores_preceding_tip"])
        self.assertFalse(measured["failover_attempts"][0]["verified_in_window"])

    def test_attribution_counts_transaction_bearing_templates_under_the_original_emitter(
        self,
    ):
        # Exercise attribution alone using synthetic template labels on a cloned snapshot.
        # The public archive command independently replays real template commitments first.
        snapshot = load(PREFIX)
        checked = qualification(snapshot, reconcile(snapshot))
        copy_rows = copy.deepcopy(snapshot)
        tips = [r for r in copy_rows["records"] if r.get("event", "tip") == "tip"]
        tips[0]["unsigned_candidate"] = {"transactions": ["fixture"]}
        tips[-2]["unsigned_candidate"] = {"transactions": []}
        tips[-1]["unsigned_candidate"] = {"transactions": ["fixture-a", "fixture-b"]}
        measured = segments(copy_rows, checked)
        self.assertEqual(measured["transaction_bearing_unsigned_templates"], 2)
        self.assertEqual(
            [s["transaction_bearing_unsigned_templates"] for s in measured["segments"]],
            [1, 0, 0, 0, 1],
        )
