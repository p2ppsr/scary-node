"""Regression cases for separate prospective and retrospective mainnet evidence."""

import copy
import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from shadow import audit
from shadow_checkpoint import (
    FINAL_CONTRACT,
    SCALE,
    checkpoint,
    current_run,
    follow,
    next_bits,
    qualification,
    reconcile,
    reference_hashes,
    replay,
    separate_report,
    target,
    timed_reports,
    verified_failover,
)


class CheckpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        evidence = (
            Path(__file__).resolve().parents[1]
            / "docs/evidence/chronicle-shadow-journal.json.gz"
        )
        with gzip.open(evidence) as source:
            cls.snapshot = json.load(source)

    def test_reference_time_clamps_and_work(self):
        bits = 0x1D00FFFF
        work = SCALE // (target(bits) + 1)
        for spacing, forecast in [(1, 0x1C7FFF80), (600, bits), (2000, bits)]:
            with self.subTest(spacing=spacing):
                self.assertEqual(
                    next_bits([(i * spacing, i * work) for i in range(147)]), forecast
                )
        for encoded in [0, 0x01000001, 0x1D80FFFF, 0x23010000]:
            with self.subTest(invalid_target=encoded), self.assertRaises(ValueError):
                target(encoded)
        with self.assertRaises(ValueError):
            next_bits([(0, work)] * 146)

    def test_replays_independent_anchor(self):
        result = reconcile(self.snapshot)
        self.assertTrue(
            result["external_reference"]["absolute_chainwork_delta_matched"]
        )
        self.assertEqual(
            result["wire_headers_rehashed"]
            + result["external_reference"]["retrospective_headers_supplemented"],
            self.snapshot["audit"]["observed_transitions"],
        )
        self.assertTrue(result["receipt_confirmed_sv_only_failover"])

    def test_failover_must_match_its_own_restored_parent(self):
        example = copy.deepcopy(self.snapshot)
        example["failover"][0]["hash"] = "00" * 32
        self.assertFalse(reconcile(example)["receipt_confirmed_sv_only_failover"])

    def test_legacy_progress_replays_but_does_not_qualify(self):
        checked = reconcile(self.snapshot)
        result = qualification(self.snapshot, checked)
        self.assertFalse(result["qualified"])
        self.assertEqual(result["templates_with_verified_successor"], 0)
        self.assertEqual(result["current_linked_blocks"], 90)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.gz"
            with self.assertRaisesRegex(ValueError, "qualification pending"):
                checkpoint(self.snapshot, path, with_reference=False, final=True)
            self.assertFalse(path.exists())
            forged = {**self.snapshot, "qualification_contract": FINAL_CONTRACT}
            path.write_bytes(gzip.compress(json.dumps(forged).encode()))
            with self.assertRaisesRegex(ValueError, "unmet shadow qualification"):
                replay(path)

    def test_an_asset_endpoint_cannot_corroborate_itself(self):
        row = {
            "height": 1,
            "hash": "a",
            "delivery_source": "asset:endpoint-1",
            "observed_at": 100,
        }
        report = {
            "event": "reference_tip",
            "height": 1,
            "hash": "a",
            "peer": "peer-1",
            "observed_at": 100,
        }
        self.assertFalse(separate_report(row, timed_reports([report, report])))
        other = {**report, "peer": "peer-2"}
        self.assertTrue(separate_report(row, timed_reports([report, other])))
        row["delivery_source"] = "sv:peer-1"
        self.assertFalse(separate_report(row, timed_reports([report])))
        self.assertTrue(separate_report(row, timed_reports([other])))
        self.assertFalse(
            separate_report(row, timed_reports([{**other, "observed_at": 7500}]))
        )

    def test_ready_tip_requires_a_full_window_and_signed_state(self):
        example = copy.deepcopy(self.snapshot)
        row = next(r for r in example["records"] if r.get("event", "tip") == "tip")
        row["ready"] = True
        example["audit"] = audit(json.dumps(r) for r in example["records"])
        with self.assertRaisesRegex(ValueError, "readiness contradicts"):
            reconcile(example)

    def test_current_window_must_own_a_witnessed_template_and_successor(self):
        # This exercises the post-replay gate. The cryptographic checks on
        # real archived headers and chainwork are covered by the replay tests.
        rows = [
            {
                "event": "tip",
                "height": i,
                "hash": f"{i:064x}",
                "previous": f"{i - 1:064x}",
                "header_hex": "wire",
                "coverage": min(i, 147),
                "ready": i >= 147,
                "pending": 0,
                "bits": 0x1D00FFFF,
                "directives": {"ready": True},
                "observed_at": i,
            }
            for i in range(1, 149)
        ]
        reports = [
            {
                "event": "reference_tip",
                "height": r["height"],
                "hash": r["hash"],
                "peer": peer,
                "observed_at": r["observed_at"],
            }
            for r in rows
            for peer in ("one", "two")
        ]
        snap = {"records": rows + reports, "failover": [], "reference": {"by_hash": {}}}
        checked = {
            "external_reference": {
                "absolute_chainwork_delta_matched": True,
                "tip_hash": rows[-1]["hash"],
                "anchor_hash": rows[0]["previous"],
            },
            "restore_mismatch_records": [],
            "continuity_or_node_prediction_exceptions": [],
            "coverage_failures_awaiting_successor": 0,
        }
        job = {"transactions": ["signed-payment"], "bits": rows[-1]["bits"]}
        with (
            patch("shadow_checkpoint.TARGET_BLOCKS", 148),
            patch("shadow_checkpoint.verified_failover", return_value=True),
        ):
            rows[-1].update(unsigned_candidate=job, pending=1)
            self.assertFalse(qualification(snap, checked)["qualified"])
            rows[-2].update(unsigned_candidate=job, pending=1)
            self.assertTrue(qualification(snap, checked)["qualified"])
            independent = checked.pop("external_reference")
            checked["external_reference"] = None
            self.assertFalse(qualification(snap, checked)["qualified"])
            checked["external_reference"] = independent
            rows[-2]["unsigned_candidate"] = None
            result = qualification(snap, checked)
            self.assertEqual(result["unexplained_missing_templates"], [147])
            rows[-1]["previous"] = "fork"
            result = qualification(snap, checked)
            self.assertEqual(result["current_linked_blocks"], 1)
            self.assertEqual(result["templates_with_verified_successor"], 0)

    def test_window_restarts_after_missing_local_evidence(self):
        # Simulated rows exercise selection of a later clean interval. Header
        # and signed-receipt verification of the real journal use the replay tests.
        oldest = current_run(self.snapshot["records"])[-1]["height"] + 1
        example = {"records": [], "failover": self.snapshot["failover"]}
        for height in range(oldest, oldest + 151):
            example["records"].append(
                {
                    "event": "tip",
                    "height": height,
                    "hash": f"{height:064x}",
                    "previous": f"{height - 1:064x}",
                    "header_hex": "fixture",
                    "coverage": 147,
                    "ready": True,
                    "pending": 1,
                    "bits": 1,
                    "unsigned_candidate": {"transactions": ["fixture"], "bits": 1},
                    "directives": {"ready": True},
                    "observed_at": height,
                }
            )
        rows = example["records"]
        rows[1]["unsigned_candidate"] = None
        rows[2]["coverage"] = 2
        checked = {
            "restore_mismatch_records": [],
            "continuity_or_node_prediction_exceptions": [],
            "coverage_failures_awaiting_successor": 0,
            "external_reference": {
                "tip_hash": rows[-1]["hash"],
                "anchor_hash": rows[0]["previous"],
                "absolute_chainwork_delta_matched": True,
            },
        }
        example["reference"] = {"by_hash": dict.fromkeys(row["hash"] for row in rows)}
        with patch("shadow_checkpoint.TARGET_BLOCKS", 148):
            result = qualification(example, checked)
            self.assertFalse(result["qualified"])
            self.assertFalse(result["failover_and_restores_in_current_run"])
            self.assertEqual(result["qualifying_window_blocks"], 148)
            self.assertEqual(result["unexplained_missing_templates"], [oldest + 1])
            self.assertEqual(result["incomplete_coverage_heights"], [oldest + 2])
            # A fresh independently journalled three-stage repeat qualifies.
            before, after = rows[4:6]
            before.update(
                receipt_trigger_hash=before["hash"], delivery_source="asset:endpoint"
            )
            after.update(receipt_trigger_hash=after["hash"], delivery_source="sv:peer")
            actions = copy.deepcopy(self.snapshot["failover"])
            actions[0].update(
                height=before["height"],
                hash=before["hash"],
                unix_s=before["observed_at"],
            )
            actions[1].update(
                height=after["height"], hash=after["hash"], unix_s=after["observed_at"]
            )
            actions[2].update(unix_s=after["observed_at"])
            example["failover"] = example["failover"] + actions
            rows.insert(
                6,
                {
                    "event": "start",
                    "body_transport": "all",
                    "at": after["observed_at"],
                    "restored_tip": after["hash"],
                },
            )
            rows.insert(
                5,
                {
                    "event": "start",
                    "body_transport": "sv_only",
                    "at": before["observed_at"],
                    "restored_tip": before["hash"],
                },
            )
            result = qualification(example, checked)
            self.assertTrue(result["qualified"], result["pending"])
            self.assertEqual(result["qualifying_window_first_height"], oldest + 3)
            self.assertEqual(result["excluded_prefix_blocks"], 3)
            self.assertEqual(result["disqualifying_records_in_linked_run"], [2, 3])
            # A partial later attempt cannot fabricate evidence by finishing an earlier one.
            self.assertFalse(
                verified_failover(rows[3:], actions[:2] + actions[:1] + actions[2:])
            )

    def test_follow_waits_for_a_prospective_successor_and_retries_external_readback(
        self,
    ):
        # Keep the real 90-block prefix and its early missing peer reports/wire
        # headers. Simulate future *local events* to test the collector state
        # machine. No synthetic event is mainnet qualification evidence.
        example = copy.deepcopy(self.snapshot)
        example.pop("reference")
        rows = example["records"]
        tips = current_run(rows)
        first = tips[0]
        template = {"transactions": ["fixture"], "bits": tips[-1]["bits"]}

        def append_local_tip():
            previous = tips[-1]
            height = previous["height"] + 1
            row = {
                "event": "tip",
                "height": height,
                "hash": f"{height:064x}",
                "previous": previous["hash"],
                "header_hex": "fixture",
                "bits": previous["bits"],
                "coverage": min(len(tips) + 1, 147),
                "ready": False,
                "pending": 0,
                "directives": {"ready": True},
                "observed_at": previous["observed_at"] + 600,
            }
            tips.append(row)
            rows.append(row)

        while len(tips) < 1008:
            append_local_tip()
        tips[-1].update(ready=True, pending=1, unsigned_candidate=template)
        summary = reconcile(self.snapshot)

        def checked(snapshot):
            checked_summary = dict(summary, external_reference=None)
            if "reference" in snapshot:
                checked_summary["external_reference"] = {
                    "anchor_hash": first["previous"],
                    "tip_hash": tips[-1]["hash"],
                    "absolute_chainwork_delta_matched": True,
                }
            return checked_summary

        attempts = []

        def reference(records):
            requested = reference_hashes(records)
            attempts.append(len(current_run(records)))
            self.assertIn(first["hash"], requested)
            if len(attempts) == 1:
                raise URLError("temporary reference outage")
            return {"by_hash": dict.fromkeys(requested)}

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            journal, receipt, output = (
                root / name for name in ("journal", "receipt", "bundle")
            )
            journal.touch()
            receipt.touch()
            pauses = []

            def wait(_):
                self.assertFalse(output.exists())
                pauses.append(len(tips))
                if len(pauses) == 1:
                    self.assertEqual(attempts, [])
                    append_local_tip()
                    # Advance the revision explicitly even on a low-resolution file system.
                    stamp = journal.stat().st_mtime_ns + 1_000_000_000
                    os.utime(journal, ns=(stamp, stamp))
                elif len(pauses) > 2:
                    self.fail("collector never retried the unchanged local snapshot")

            with (
                patch("shadow_checkpoint.export", return_value=example),
                patch("shadow_checkpoint.reconcile", side_effect=checked),
                patch("shadow_checkpoint.public_reference", side_effect=reference),
                patch("shadow_checkpoint.time.sleep", side_effect=wait),
            ):
                result = follow(journal, receipt, output)
                self.assertEqual(replay(output, assess=True), result)
            self.assertEqual(attempts, [1009, 1009])
            self.assertEqual(pauses, [1008, 1009])
            self.assertTrue(result["qualification"]["qualified"])
            self.assertEqual(result["qualification"]["qualifying_window_blocks"], 1009)
            with gzip.open(output) as source:
                saved = json.load(source)
            self.assertEqual(
                saved["records"][: len(self.snapshot["records"])],
                self.snapshot["records"],
            )
            self.assertFalse(
                saved["records"][saved["records"].index(first)].get("header_hex")
            )

    def test_corrupt_reference_chainwork(self):
        example = copy.deepcopy(self.snapshot)
        ref = next(iter(example["reference"]["by_hash"].values()))
        ref["chainwork"] = "0"
        with self.assertRaisesRegex(ValueError, "work"):
            reconcile(example)

    def test_stale_signed_receipt(self):
        example = copy.deepcopy(self.snapshot)
        row = next(
            r for r in example["records"] if r.get("directives", {}).get("ready")
        )
        row["directives"]["last_peer_sync"]["unix_s"] = row["observed_at"] - 1201
        with self.assertRaisesRegex(ValueError, "signed prefix"):
            reconcile(example)


if __name__ == "__main__":
    unittest.main()
