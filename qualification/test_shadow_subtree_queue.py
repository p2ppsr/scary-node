import copy
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from shadow_subtree_queue import confirmed, describe, trace


class SubtreeTraceTest(unittest.TestCase):
    def test_shared_stdout_stderr_and_unrecoverable_frame(self):
        a = b"0" * 64
        b = b"1" * 64

        def frame(k, p, full):
            return (
                b'{"event":"subtree_notice","hash":"'
                + k
                + b'","peer":"'
                + p
                + b'","queue_full":'
                + full
                + b"}"
            )

        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "observer.log"
            path.write_bytes(
                b'{"event":"started","network":"mainnet"}\n'
                + b"pending "
                + frame(a, b"ab", b"true")
                + b" diagnostic\n"
                + frame(b, b"ab", b"false")
                + b"\n"
                + frame(a, b"cd", b"false")
                + b"\n"
            )
            observed = trace(path, 1, 3)
            r = describe(observed["rows"], [])
            self.assertEqual(
                (
                    observed["mixed_lines"],
                    r["distinct_roots"],
                    r["roots_seen_across_publishers"],
                    r["first_full_then_nonfull_roots"],
                ),
                (1, 2, 1, 1),
            )
            with path.open("ab") as f:
                f.write(b'{"event":"subtree_notice","hash":broken}\n')
            with self.assertRaisesRegex(ValueError, "incomplete or unrecognized"):
                trace(path, 1, 4)

    def test_duplicate_full_is_one_root_and_waiting_slots_bound_completions(self):
        rows = [[k, 0, 0, 1] for k in range(20)] + [[20, 0, 1, 1], [20, 1, 1, 1]]
        r = describe(rows, [])
        self.assertEqual(
            (r["full_notices"], r["only_full_roots"], r["successful_root_lower_bound"]),
            (2, 1, 3),
        )
        self.assertEqual(describe(rows, [5])["successful_root_lower_bound"], 2)

    def test_sealed_public_trace_requires_valid_stock_header_and_commitment(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "docs/evidence/chronicle-shadow-subtree-queue.json.gz"
        )
        with gzip.open(path, "rb") as stream:
            data = json.load(stream)
        rows = data["trace"]["rows"][:6003]
        keys = data["trace"]["root_commitments"]
        blocks = data["confirmed"]
        observed = confirmed(rows, keys, blocks)
        hits = [
            (s["height"], hit)
            for s in observed
            for hit in s["confirmed_content_matches"]
        ]
        self.assertEqual(len({hit["root"] for _, hit in hits}), 21)
        self.assertTrue(
            all(
                hit["offer_flags"] == [1] and hit["first_seen_through_height"] < height
                for height, hit in hits
            )
        )
        for field, reason in (
            ("reference_subtrees", "partition differs"),
            ("transaction_ids", "transaction commitment"),
            ("reference_header", "stock metadata"),
        ):
            invalid = copy.deepcopy(blocks)
            if field == "reference_header":
                invalid[0][field]["nonce"] += 1
            else:
                invalid[0][field][0] = "00" * 32
            with self.assertRaisesRegex(ValueError, reason):
                confirmed(rows, keys, invalid)
