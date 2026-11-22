"""Closed-cohort descriptive subgroup accounting on synthetic determinate rows."""

import tempfile
import unittest
from pathlib import Path

from matched_subgroups import GROUPS, analyze, group_counts, record, tip_disposition


class MatchedSubgroups(unittest.TestCase):
    def row(self, found, alignment, winning):
        lanes = [
            {
                "ready": exists,
                "hashes": int(exists),
                "discovery": {"hash": h} if exists else None,
            }
            for h, exists in zip(("left", "right"), found)
        ]
        observed = [
            {
                "active_hash": "left",
                "bodies": {
                    lane["discovery"]["hash"]: {
                        "body_sha256": "digest",
                        "attempts": [{}],
                    }
                    for lane in lanes
                    if lane["discovery"]
                },
            },
            {
                "active_hash": None,
                "bodies": {
                    lane["discovery"]["hash"]: {
                        "readback_unavailable": True,
                        "attempts": [{}, {}],
                    }
                    for lane in lanes
                    if lane["discovery"]
                },
            },
        ]
        return {
            "lanes": lanes,
            "hold_ns": 300,
            "ingress_alignment": [{"status": s} for s in alignment],
            "settlement": {"depth_two_ancestry": [winning, winning]},
            "session_final_ancestry": [winning, winning],
            "predeadline_readbacks": observed,
            "postdeadline_readbacks": observed,
        }

    def test_all_exposures_and_failures_partition_ties_with_placebo_ready(self):
        counts = {g: group_counts() for g in GROUPS}
        for row in [
            self.row([True, True], ["certified"] * 2, "right"),
            self.row([True, True], ["timing_not_certified", "certified"], "left"),
            self.row([True, False], ["single_or_no_discovery"] * 2, "left"),
            self.row([False, False], ["single_or_no_discovery"] * 2, "other"),
        ]:
            record(row, counts)
        all_ = counts["all_assigned"]
        self.assertEqual(all_["parents"], 4)
        self.assertEqual(all_["lane"][1]["scheduled"], 4)
        self.assertEqual(all_["lane"][1]["discovered"], 2)
        self.assertEqual(all_["selected_at_depth_two"]["continuation_only"], 1)
        self.assertEqual(all_["receivers"][1]["predeadline_active_unknown"], 4)
        self.assertEqual(all_["receivers"][1]["postdeadline_transport_exhausted"], 5)
        self.assertEqual(counts["all_ties"]["parents"], 2)
        self.assertEqual(
            counts["both_receivers_certified_ties"]["selected_at_depth_two"]["right"], 1
        )
        self.assertEqual(counts["other_ties"]["selected_at_depth_two"]["left"], 1)

    def test_receiver_selection_distinguishes_split_tips_and_bounded_order(self):
        row = self.row([True, True], ["certified"] * 2, "right")
        row["gate_complete_ns"] = 100
        row["plan"] = {"successor_receiver": 1}
        row["settlement"].update(
            precontinuation_tips=["left", "right"], generation_started_ns=125
        )
        for i, choice in enumerate(("left", "right")):
            row["postdeadline_readbacks"][i].update(
                active_hash=choice, active_query_ns=[105, 110]
            )
        row["ingress_alignment"][1]["left_minus_right_ingress_bounds_ns"] = [1, 6]
        self.assertEqual(
            tip_disposition(row), (("split", "right", "right"), True, True, True)
        )
        row["ingress_alignment"][1]["left_minus_right_ingress_bounds_ns"] = [-3, 0]
        self.assertEqual(
            tip_disposition(row)[0], ("split", "interval_contains_zero", "right")
        )
        row["settlement"]["precontinuation_tips"] = ["parent", "parent"]
        del row["ingress_alignment"][1]["left_minus_right_ingress_bounds_ns"]
        self.assertEqual(
            tip_disposition(row),
            (("outside_candidate_pair", "unavailable", "right"), False, False, True),
        )
        row["settlement"]["precontinuation_tips"][1] = None
        self.assertEqual(tip_disposition(row)[0][0], "unavailable")
        row["postdeadline_readbacks"][1]["active_query_ns"][1] = 126
        with self.assertRaisesRegex(ValueError, "crosses continuation"):
            tip_disposition(row)

    def test_incomplete_phase_cannot_be_read_or_reported(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(ValueError, "not closed"),
        ):
            analyze(Path(tmp), Path(tmp) / "registration.json")
