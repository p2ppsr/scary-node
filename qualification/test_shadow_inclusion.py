"""Regression tests for selection and independently committed successor transaction lists."""

import copy
import gzip
import json
from pathlib import Path
import unittest

from shadow import digest, merkle
from shadow_checkpoint import current_run
from shadow_inclusion import compare, read_block


class InclusionTests(unittest.TestCase):
    def setUp(self):
        with gzip.open(
            Path(__file__).resolve().parents[1]
            / "docs/evidence/chronicle-shadow-journal.json.gz"
        ) as source:
            parent, child = current_run(json.load(source)["records"])[-2:]
        # The observed headers and successor IDs are real. The unsigned choice
        # below is solely a test fixture; the observer was still filling its window.
        self.txs = [
            "fb767d6fe19d787b43eeeb2528f546011503b1e2afee892140c1d3c855bf6b31",
            "2214b7e0d61ad583f0a12e648703f6616c589c5ff6d51501939d9d2c8fdbf84f",
            "a5b66170b42d39f9aa255b66c041721a061530db1ba87e7c75ecdb79612d2f4b",
            "099ca907253557a86880eebf2db3bb2f91a1e0ff600a6262b16c3d45fb15a68c",
        ]
        self.snapshot = {"records": [parent, child]}
        choices = [self.txs[1], "ab" * 32]
        header = bytearray.fromhex(child["header_hex"])
        header[36:68] = merkle(
            [digest(b"fixture")] + [bytes.fromhex(x)[::-1] for x in choices]
        )
        parent.update(
            ready=True,
            pending=2,
            expected_next_bits=child["bits"],
            unsigned_candidate={
                "transactions": choices,
                "height": child["height"],
                "header_hex": header.hex(),
                "bits": child["bits"],
                "coinbase": b"fixture".hex(),
            },
        )
        self.child = child
        self.row = {
            "hash": child["hash"],
            "height": child["height"],
            "transactions": self.txs,
            "confirmed_at_read": 1,
        }

    def test_merkle_checked_counts_include_absent_choices(self):
        report = compare(self.snapshot, [self.row])
        self.assertEqual(
            (
                report["measured_pairs"],
                report["candidate_transaction_opportunities"],
                report["also_in_immediate_successor"],
                report["sample_complete"],
            ),
            (1, 2, 1, False),
        )
        for changes in (
            {"transactions": self.txs[:-1]},
            {"transactions": self.txs[::-1]},
            {"transactions": self.txs + [self.txs[-1]]},
            {"confirmed_at_read": 0},
        ):
            with self.assertRaises(ValueError):
                compare(self.snapshot, [dict(self.row, **changes)])
        no_job = copy.deepcopy(self.snapshot)
        no_job["records"][0]["unsigned_candidate"] = None
        self.assertEqual(compare(no_job, [])["measured_pairs"], 0)
        with self.assertRaises(ValueError):
            compare(no_job, [self.row])

    def test_archived_reference_lists_match_both_real_header_commitments(self):
        evidence = Path(__file__).resolve().parents[1] / "docs/evidence"
        with gzip.open(evidence / "chronicle-shadow-journal.json.gz") as source:
            tips = {
                row["hash"]: row for row in current_run(json.load(source)["records"])
            }
        with gzip.open(
            evidence / "chronicle-shadow-reference-transactions.json.gz"
        ) as source:
            rows = json.load(source)
        self.assertEqual([len(row["transactions"]) for row in rows], [668, 4])
        for row in rows:
            tip = tips[row["hash"]]
            self.assertEqual(tip["height"], row["height"])
            self.assertGreater(row["confirmed_at_read"], 0)
            self.assertEqual(
                merkle([bytes.fromhex(tx)[::-1] for tx in row["transactions"]]),
                bytes.fromhex(tip["header_hex"])[36:68],
            )

    def test_pagination_checks_order_prefix_and_reference_tip(self):
        route = "/block/hash/" + self.child["hash"]
        pages = [route + "/page/1", route + "/page/2"]
        block = {
            "hash": self.child["hash"],
            "height": self.child["height"],
            "confirmations": 2,
            "txcount": 4,
            "tx": self.txs[:1],
            "pages": {"uri": pages},
        }
        reference = {route: block, pages[0]: self.txs[:2], pages[1]: self.txs[2:]}
        self.assertEqual(
            read_block(self.child, reference.__getitem__)["transactions"], self.txs
        )
        suffix_pages = dict(reference)
        suffix_pages[pages[0]] = self.txs[1:2]
        self.assertEqual(
            read_block(self.child, suffix_pages.__getitem__)["transactions"], self.txs
        )
        for patch in (
            {"confirmations": 0},
            {"tx": self.txs[1:2]},
            {"pages": {"uri": pages[::-1]}},
        ):
            with self.assertRaises(ValueError):
                read_block(
                    self.child,
                    dict(reference, **{route: dict(block, **patch)}).__getitem__,
                )


if __name__ == "__main__":
    unittest.main()
