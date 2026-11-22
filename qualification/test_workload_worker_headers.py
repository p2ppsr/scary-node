"""Keep worker response failures and active-chain proof as independent observations."""

import copy
import gzip
import json
import socket
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from workload_worker_headers import audit

FIXTURE = (
    Path(__file__).resolve().parents[1] / "docs/evidence/chronicle-retained-job.json.gz"
)


class WorkerHeaderAttribution(unittest.TestCase):
    def setUp(self):
        with gzip.open(FIXTURE, "rt") as f:
            self.original = json.load(f)
        r = self.original
        candidate = {
            "notify": r["worker"]["first_job"],
            "extranonce1": r["worker"]["extranonce1"],
            "extranonce2": r["extranonce2"],
            "header": r["winning_wire"][:160],
            "hash": r["winner"],
        }
        self.slot = {
            "index": 0,
            "phase": "offer",
            "miner": "scary",
            "parent": r["parent"],
            "ready": True,
            "outcome": "error",
            "worker_candidate": candidate,
        }
        self.record = {
            "worker_header_format": 1,
            "slots": [self.slot],
            "blocks": [
                {
                    "height": r["height"],
                    "hash": r["winner"],
                    "header_hex": candidate["header"],
                    "scheduled_payments": 1,
                }
            ],
        }

    def test_exact_header_is_selected_after_missing_reply(self):
        report = audit(self.record)
        self.assertEqual(report["prepared_attempts_by_worker_outcome"], {"error": 1})
        self.assertEqual(report["selected_blocks"][0]["hash"], self.original["winner"])
        self.assertEqual(report["selected_payments"], 1)
        self.assertEqual(report["selected_without_worker_acknowledgment"], 1)
        self.assertEqual(report["primary_intervals"][0]["selected_nonempty"], 1)

    def test_repeated_attempt_is_one_block_with_both_response_dispositions(self):
        retry = dict(
            self.slot,
            phase="drain",
            index=300,
            outcome="solved",
            hash=self.original["winner"],
        )
        self.record["slots"].extend([retry, {"miner": "scary", "outcome": "missed"}])
        report = audit(self.record)
        self.assertEqual(
            report["prepared_attempts_by_worker_outcome"], {"solved": 1, "error": 1}
        )
        self.assertEqual(report["attempts_without_a_prepared_header"], {"missed": 1})
        self.assertEqual(len(report["selected_blocks"][0]["attempts"]), 2)
        self.assertEqual(
            report["repeated_headers_across_intervals_or_phases"],
            [self.original["winner"]],
        )
        self.assertEqual(report["primary_selected_payments"], 0)
        self.assertEqual(report["selected_payments"], 1)

    def test_job_must_reproduce_the_actual_header(self):
        self.slot["worker_candidate"]["extranonce1"] = "ab" * 8
        with self.assertRaisesRegex(ValueError, "differs from its Stratum job"):
            audit(self.record)

    def test_solved_response_cannot_drop_header(self):
        self.slot["outcome"] = "solved"
        del self.slot["worker_candidate"]
        with self.assertRaisesRegex(ValueError, "missing its prepared header"):
            audit(self.record)

    def test_worker_keeps_exact_candidate_when_peer_closes_after_submit(self):
        from sustained_native import stratum

        started = self.original["worker"]
        slot = copy.deepcopy(self.slot)
        del slot["worker_candidate"]
        client, server = socket.socketpair()

        def serve():
            with server, server.makefile("rwb") as stream:
                server.settimeout(4)
                subscribe = json.loads(stream.readline())
                authorize = json.loads(stream.readline())
                for message in (
                    {
                        "id": subscribe["id"],
                        "result": [
                            [],
                            started["extranonce1"],
                            started["extranonce2_bytes"],
                        ],
                        "error": None,
                    },
                    {"id": authorize["id"], "result": True, "error": None},
                    {"id": None, "method": "mining.set_difficulty", "params": [1]},
                    {
                        "id": None,
                        "method": "mining.notify",
                        "params": started["first_job"],
                    },
                ):
                    stream.write(json.dumps(message).encode() + b"\n")
                stream.flush()
                return json.loads(stream.readline())

        with ThreadPoolExecutor(max_workers=1) as thread:
            received = thread.submit(serve)
            with (
                patch("sustained_native.socket.create_connection", return_value=client),
                self.assertRaisesRegex(EOFError, "session closed"),
            ):
                stratum(self.original["parent"], slot)
            submission = received.result()
        self.assertEqual(submission["method"], "mining.submit")
        proof = slot["worker_candidate"]
        self.assertEqual(submission["params"][1], proof["notify"][0])
        self.assertEqual(
            proof["header"][-8:], bytes.fromhex(submission["params"][-1])[::-1].hex()
        )
        self.record["slots"] = [slot]
        self.record["blocks"] = []
        self.assertEqual(
            audit(self.record)["prepared_headers_outside_active_prefix"], 1
        )


if __name__ == "__main__":
    unittest.main()
