"""Replay the complete published solution ledger including the absent publication."""

import unittest
from pathlib import Path

from workload_publication import analyze, docker_events, load

EVIDENCE = Path(__file__).resolve().parents[1] / "docs/evidence"


class PublicationTiming(unittest.TestCase):
    def test_reordered_positive_log_and_worker_returns_are_independent(self):
        base = EVIDENCE / "chronicle-sustained-reordered"
        with Path(str(base) + "-gossip.txt").open() as stream:
            events = docker_events(stream)
        report = analyze(
            load(base.with_suffix(".json.gz")),
            events,
            load(Path(str(base) + "-ledger.json")),
        )
        groups = report["groups"]
        self.assertEqual(report["total_log_entries"], 79)
        self.assertEqual(groups["all_local"]["solutions"], 56)
        self.assertEqual(groups["all_local"]["with_positive_log"], 55)
        absent = [row for row in report["solutions"] if not row["log_entries"]]
        self.assertEqual(
            [(r["worker_index"], r["worker_outcome"], r["selected"]) for r in absent],
            [(140, "solved", False)],
        )
        recovered = [
            r
            for r in report["solutions"]
            if r["selected"] and r["worker_outcome"] == "error"
        ]
        self.assertEqual(sorted(r["height"] for r in recovered), [524, 550, 568, 577])
        last = next(r for r in recovered if r["height"] == 577)
        self.assertTrue(
            all(
                t is None for t in last["stock_tip_sample_minus_worker_end_ms"].values()
            )
        )
        self.assertGreater(
            last["local_queue_to_first_sv_delivery_receipt_ms"]["sent_body"], 0
        )
