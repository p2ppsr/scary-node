"""Accounting regressions for the native payment workload and its resource sampler."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verify_sustained_native as audit
from sustained_resources import DOCKER_MIN, HOST_MIN, fields


class SustainedVerifierTests(unittest.TestCase):
    def setUp(self):
        receipt = (
            Path(__file__).resolve().parents[1]
            / "docs/evidence/chronicle-asset-framing.json"
        )
        self.body = bytes.fromhex(
            json.loads(receipt.read_text())["reference_block_hex"]
        )
        h = audit.dsha(self.body[:80])[::-1].hex()
        _, txs, _ = audit.decode_block(self.body, h)
        self.payment = txs[1]
        self.txid = audit.dsha(self.payment)
        self.block = {
            "header_hex": self.body[:80].hex(),
            "hash": h,
            "wire_bytes": len(self.body),
            "wire_sha256": hashlib.sha256(self.body).hexdigest(),
            "ordinary_transactions": 1,
            "scheduled_payments": 1,
            "duplicate_scheduled": 0,
            "foreign_payments": 0,
            "confirmed_by_source_cohort": [0, 0, 0, 1, 0, 0],
        }

    def test_merkle_committed_membership_distinct_across_active_blocks(self):
        seen = bytearray(6)
        audit.verify_block_membership(self.body, self.block, {self.txid: 3}, seen, 1)
        self.assertEqual(seen, bytes([0, 0, 0, 1, 0, 0]))
        with self.assertRaisesRegex(ValueError, "membership"):
            audit.verify_block_membership(
                self.body, self.block, {self.txid: 3}, seen, 1
            )
        duplicate = {
            **self.block,
            "scheduled_payments": 0,
            "duplicate_scheduled": 1,
            "confirmed_by_source_cohort": [0] * 6,
        }
        audit.verify_block_membership(self.body, duplicate, {self.txid: 3}, seen, 1)
        foreign = {**duplicate, "foreign_payments": 1, "duplicate_scheduled": 0}
        audit.verify_block_membership(self.body, foreign, {}, seen, 1)

    def test_root_checked_after_wire_digest_matches(self):
        damaged = self.body[:-1] + bytes([self.body[-1] ^ 1])
        replacement = {**self.block, "wire_sha256": hashlib.sha256(damaged).hexdigest()}
        with self.assertRaisesRegex(ValueError, "Merkle"):
            audit.verify_block_membership(
                damaged, replacement, {self.txid: 3}, bytearray(6), 1
            )

    def test_source_envelope_checks_all_three_transaction_id_commitments(self):
        raw = self.payment
        header = (
            audit.MAGIC
            + audit.TX_COMMAND
            + len(raw).to_bytes(4, "little")
            + self.txid[:4]
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.multiple(audit, BATCHES=1, PAYMENTS=1, BATCH_SIZE=1),
        ):
            d = Path(tmp)
            source = {
                "native_wire_bytes": len(header) + len(raw),
                "offered_id_sha256": hashlib.sha256(self.txid).hexdigest(),
            }
            (d / "manifest.json").write_text(json.dumps(source))
            (d / "offsets.json").write_text(json.dumps([0, len(header) + len(raw)]))
            (d / "ids.bin").write_bytes(self.txid)
            (d / "frames.bin").write_bytes(header + raw)
            self.assertEqual(audit.read_source(d, source), {self.txid: 0})
            (d / "ids.bin").write_bytes(self.txid[::-1])
            with self.assertRaisesRegex(ValueError, "ordered ID"):
                audit.read_source(d, source)
            (d / "ids.bin").write_bytes(self.txid)
            (d / "frames.bin").write_bytes(header[:-1] + bytes([header[-1] ^ 1]) + raw)
            with self.assertRaisesRegex(ValueError, "envelope"):
                audit.read_source(d, source)

    def test_incomplete_attempt_preserves_missing_offers_and_failed_accounting(self):
        base = {"height": 108, "hash": self.block["hash"]}
        record = {
            "schema": 1,
            "network": "regtest",
            "registration": audit.REGISTRATION,
            "release_sha256": audit.REFERENCE_RELEASE,
            "source": {
                "scheduled": audit.PAYMENTS,
                "batch_size": 512,
                "batches": 10800,
                "offered_id_sha256": "01" * 32,
            },
            "duration_ns": 123456,
            "producer": [],
            "slots": [],
            "samples": [],
            "resources": [],
            "blocks": [],
            "baseline": base,
            "completion": {
                "unique_confirmed": 0,
                "missing": audit.PAYMENTS,
                "duplicate_confirmations": 0,
                "last_payment_height": 108,
                "final_height": 108,
                "final_hash": base["hash"],
                "all_accounted": False,
                "primary_complete": False,
                "two_successors": False,
                "resource_stop": True,
                "audit_failure": None,
            },
        }
        summary = audit.verify(record)
        self.assertFalse(summary["adoption_target_met"])
        self.assertEqual(summary["missing"], audit.PAYMENTS)
        altered = copy.deepcopy(record)
        altered["completion"]["primary_complete"] = True
        with self.assertRaisesRegex(ValueError, "opportunities"):
            audit.verify(altered)
        altered = copy.deepcopy(record)
        altered["release_sha256"] = "ff" * 32
        with self.assertRaisesRegex(ValueError, "release"):
            audit.verify(altered)

    def test_native_request_and_serve_memberships_are_counted_independently(self):
        requested = bytes([1, 1, 0, 1])
        served = bytes([0, 1, 0, 1])
        counters = dict(
            requests=5,
            served=2,
            unknown_requests=1,
            unique_requested=3,
            unique_served=2,
            served_wire_bytes=450,
        )
        final = dict(
            counters,
            requested_bits_sha256=hashlib.sha256(requested).hexdigest(),
            served_bits_sha256=hashlib.sha256(served).hexdigest(),
        )
        self.assertEqual(
            audit.inventory_evidence([dict(counters, elapsed_ns=100)], final)[
                "requests"
            ],
            5,
        )
        with self.assertRaisesRegex(ValueError, "regressed"):
            audit.inventory_evidence(
                [dict(counters, elapsed_ns=100)], dict(final, requests=4)
            )
        with tempfile.TemporaryDirectory() as name:
            d = Path(name)
            for k, v in (("requested", requested), ("served", served)):
                (d / (k + ".bits")).write_bytes(v)
            summary = audit.inventory_membership(d, final, bytes([1, 1, 0, 0]))
            self.assertEqual(
                summary,
                {
                    "confirmed_and_wallet_served": 1,
                    "confirmed_without_recorded_wallet_serve": 1,
                },
            )
            (d / "served.bits").write_bytes(bytes([0, 1, 1, 0]))
            with self.assertRaisesRegex(ValueError, "differs"):
                audit.inventory_membership(d, final, bytes(4))

    def test_stock_gauges_keep_queue_separate_from_pool_and_show_actual_coverage(self):
        rows = [
            {
                "wall_ns": 1000,
                "elapsed_ns": 200,
                "finished_wall_ns": 1030,
                "getmempoolinfo": {"size": 16},
                "getblockchainactivity": {"transactions": 900},
                "getorphaninfo": {"size": 0},
            }
        ]
        s = audit.stock_activity(rows, 800)
        self.assertEqual(s["getblockchainactivity.transactions"]["maximum"], 900)
        self.assertEqual(s["getmempoolinfo.size"]["last"], 16)
        with self.assertRaisesRegex(ValueError, "timestamps"):
            audit.stock_activity(rows, 0)

    def test_resource_samples_preserve_read_time_and_floor_crossing(self):
        sample = fields("110 2048 3072 200 18 4096", 5)
        self.assertEqual(
            (sample["filesystem_free_bytes"], sample["namespace_objects"]), (204800, 18)
        )
        row = {
            "elapsed_ns": 9,
            "services": {"as1": sample},
            "host_filesystem_free_bytes": HOST_MIN,
            "docker_filesystem_free_bytes": DOCKER_MIN,
            "stop_reason": None,
        }
        self.assertEqual(
            audit.resources([row])["services"]["as1"]["distinct_samples"], 1
        )
        with self.assertRaisesRegex(ValueError, "reserve"):
            audit.resources([{**row, "host_filesystem_free_bytes": HOST_MIN - 1}])


if __name__ == "__main__":
    unittest.main()
