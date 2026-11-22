import unittest
from pathlib import Path
from unittest.mock import patch

from recover_sustained_chain import read
from workload_settlement import trace


class SettlementTraceTests(unittest.TestCase):
    def test_original_group_audit_is_reproduced_exactly(self):
        data = Path(__file__).resolve().parents[1] / "docs/evidence"
        self.assertEqual(
            trace(read(data / "chronicle-sustained-reordered.json.gz")),
            read(data / "chronicle-sustained-reordered-settlement.json"),
        )

    def test_preserves_separate_peer_reads_and_timed_out_prepared_work(self):
        times = {
            p: {"start_ns": n, "end_ns": n + 1}
            for n, p in enumerate(("sv", "tn1", "tn2"))
        }
        record = {
            "producer": [],
            "completion": {"last_payment_height": 8},
            "start_wall_ns": 1,
            "duration_ns": 30,
            "phase_timing_format": 1,
            "worker_header_format": 1,
            "slots": [
                {
                    "index": 7,
                    "phase": "offer",
                    "outcome": "error",
                    "worker_candidate": {"notify": ["job-7"], "hash": "selected"},
                    "worker_submit_started_ns": 1,
                    "worker_submit_ended_ns": 3,
                }
            ],
            "blocks": [
                {
                    "height": 8,
                    "hash": "selected",
                    "scheduled_payments": 1,
                    "wire_bytes": 200,
                    "independent_wire_sha256": {},
                    "first_observed_ns": {},
                    "readback_ns": 8,
                    "independent_wire_read": times,
                }
            ],
        }
        checked = {
            "registered_payments": 1,
            "distinct_confirmed": 1,
            "seconds_through_end": 1.0,
            "confirmed_spends_per_second": 1.0,
        }
        with patch("workload_settlement.verify", return_value=checked):
            [body] = trace(record)["last_payment_and_successors"]
            self.assertIsNone(body["scheduled_solution_slot"])
            self.assertEqual(body["independent_wire_read"], times)
            self.assertEqual(
                body["prepared_worker_attempts"],
                [
                    {
                        "phase": "offer",
                        "index": 7,
                        "job": "job-7",
                        "outcome": "error",
                        "submit_started_ns": 1,
                        "submit_ended_ns": 3,
                    }
                ],
            )
            checked["distinct_confirmed"] = 0
            self.assertEqual(trace(record)["last_payment_and_successors"], [])


if __name__ == "__main__":
    unittest.main()
