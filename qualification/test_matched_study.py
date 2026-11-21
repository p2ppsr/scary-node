"""Check prospective allocation and deadline evidence without drawing any race outcomes."""

import copy
import gzip
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from cohort_archive import canonical
from matched_archive import sessions, write_shards
from matched_study import (
    ARMS,
    atom,
    close_phase,
    phase_bounds,
    schedule,
    summarize,
    verify_reads,
)


class ProspectiveStudyTests(unittest.TestCase):
    def setUp(self):
        self.d = {
            "hold_ns": 200_000_000,
            "tolerance_ns": 5_000_000,
            "capture_window_ns": 500_000_000,
            "pong_timeout_ns": 250_000_000,
            "signed_delay_ns": 40_000_000,
            "probe_lead_ns": 100_000_000,
            "offer_delay_ms": 1200,
            "job_response_window_ns": 1_500_000_000,
            "target_half_width": 0.01,
            "seed_base_decimal": str(1 << 165),
            "binary_sha256": "f" * 64,
        }

    def test_packed_session_members_match_individual_raw_and_stream_on_standard_gzip(
        self,
    ):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            raw = root / "raw"
            packed = root / "packed"
            names = [f"{i}.json.gz" for i in range(65)]
            for i, name in enumerate(names):
                atom(raw / name, gzip.compress(canonical({"row": i}), mtime=0))
            write_shards(raw, packed, names, atom)
            self.assertEqual(list(sessions(raw, names)), list(sessions(packed, names)))
            with gzip.open(packed / "sessions-00000.jsonl.gz", "rt") as file:
                self.assertEqual(
                    [json.loads(line)["row"] for line in file], list(range(64))
                )
            shard = packed / "sessions-00001.jsonl.gz"
            atom(shard, shard.read_bytes() + b"x")
            with self.assertRaisesRegex(ValueError, "shard hash"):
                list(sessions(packed, names))

    def test_balanced_reproducible_disjoint_schedules(self):
        start, n = phase_bounds(self.d, "pilot", None)
        _, plans = schedule(self.d, start)
        self.assertEqual(n, 16)
        self.assertEqual(schedule(self.d, start)[1], plans)
        self.assertNotEqual(schedule(self.d, start + 1)[1], plans)
        conditions = Counter(
            (p["arm"], p["timing"]["offsets_ns"][0] - p["timing"]["offsets_ns"][1])
            for p in plans
        )
        self.assertEqual(
            conditions,
            {(arm, off): 6 for arm in ARMS for off in (-40_000_000, 0, 40_000_000)},
        )
        preset = {
            "phase": "pilot",
            "sessions": 16,
            "parents": 576,
            "binary_sha256": "f" * 64,
            "fresh_evaluation_sessions": 53,
        }
        self.assertEqual(phase_bounds(self.d, "evaluation", preset), (start + 16, 53))

    def test_partial_data_cannot_be_analyzed_and_zero_denominator_cannot_size(self):
        with (
            tempfile.TemporaryDirectory() as t,
            self.assertRaisesRegex(ValueError, "incomplete"),
        ):
            close_phase(Path(t), {"design": self.d}, "pilot")
        empty = {
            i: {
                f"{a}:{o}ns": {
                    "parents": 6,
                    "alignment": Counter(single_or_no_discovery=6),
                    "lanes": [Counter(scheduled=6), Counter(scheduled=6)],
                }
                for a in ARMS
                for o in (-40_000_000, 0, 40_000_000)
            }
            for i in range(16)
        }
        s = summarize(empty, self.d, "pilot")
        self.assertEqual(s["parents"], 576)
        self.assertIsNone(s["fresh_evaluation_sessions"])
        self.assertFalse(
            s["equal_assigned_certification"]["witnessed/full"]["met_95_percent"]
        )
        with self.assertRaisesRegex(ValueError, "evaluation size"):
            phase_bounds(self.d, "evaluation", {**s, "binary_sha256": "f" * 64})

    def test_body_receipts_use_actual_query_chronology_after_sorted_json(self):
        sample = {
            "active_hash": "p",
            "active_query_ns": [50, 51],
            "bodies": {
                "a": {"rpc_code": -5, "query_ns": [60, 65]},
                "z": {"body_sha256": "z-digest", "query_ns": [53, 56]},
            },
        }
        sample["active_receipt"] = {
            "active_hash": "p",
            "query_ns": [50, 51],
            "attempts": [{"result": "observed", "query_ns": [50, 51]}],
        }
        for body in sample["bodies"].values():
            result = (
                {"result": "observed"}
                if "body_sha256" in body
                else {"result": "rpc_error", "code": -5}
            )
            body["attempts"] = [{**result, "query_ns": body["query_ns"]}]
        row = {
            "capture_window_ns": 50,
            "pong_timeout_ns": 10,
            "plan": {"probe_lead_ns": 10},
            "lanes": [
                {"discovery": {"hash": s, "body_sha256": s + "-digest"}}
                for s in ("a", "z")
            ],
            "parent": "p",
            "predeadline_readbacks": [sample, copy.deepcopy(sample)],
        }
        verify_reads(row, "predeadline_readbacks")
        row["predeadline_readbacks"][1]["active_query_ns"] = [49, 50]
        row["predeadline_readbacks"][1]["active_receipt"]["query_ns"] = [49, 50]
        row["predeadline_readbacks"][1]["active_receipt"]["attempts"][0]["query_ns"] = [
            49,
            50,
        ]
        with self.assertRaisesRegex(ValueError, "assigned times"):
            verify_reads(row, "predeadline_readbacks")


if __name__ == "__main__":
    unittest.main()
