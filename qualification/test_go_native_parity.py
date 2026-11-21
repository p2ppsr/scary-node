"""A shared release version cannot substitute for a native archive's revision or case outcome."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from go_native_parity import sha, verify
from linux_go_parity import verify_linux
from linux_shadow_candidate import checked_summary, origin_in_journal
from shadow_bdk import warmup
from shadow_first_candidate import load


class GoProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        evidence = Path(__file__).resolve().parents[1] / "docs/evidence"
        corpus = evidence / "chronicle-waiting-entrant-corpus.json.gz"
        manifest = evidence / "chronicle-go-native-macos.json"
        cls.args = (
            load(corpus),
            json.loads(manifest.read_text()),
            sha(corpus),
            sha(manifest),
        )
        cls.record = load(evidence / "chronicle-go-native-parity.json.gz")

    def test_native_revision_and_live_outcome_are_independently_bound(self):
        positive = verify(self.record, *self.args)
        self.assertEqual(positive["new_go_decisions"], 248)
        self.assertEqual(positive["disagreements_with_live_stock"], [])
        wrong = copy.deepcopy(self.record)
        pair = wrong["rows"][0]["results"]
        pair["source"]["source"] = pair["distributed"]["source"]
        with self.assertRaisesRegex(ValueError, "native revision"):
            verify(wrong, *self.args)
        modified = copy.deepcopy(self.record)
        m = modified["rows"][0]["results"]["source"]
        m["policy"] = not m["policy"]
        self.assertEqual(
            verify(modified, *self.args)["disagreements_with_live_stock"],
            [
                {
                    "txid": modified["rows"][0]["txid"],
                    "mode": "policy",
                    "variant": "source",
                }
            ],
        )

    def test_matching_false_value_requires_the_original_script_rejection(self):
        changed = copy.deepcopy(self.record)
        rejection = next(
            r["results"]["source"]
            for r in changed["rows"]
            if not r["results"]["source"]["consensus"]
        )
        rejection["consensus_error"] = "native validator was interrupted"
        with self.assertRaisesRegex(ValueError, "script reason"):
            verify(changed, *self.args)

    def test_arm64_receipt_requires_the_frozen_image_elf_and_both_runtime_archives(
        self,
    ):
        evidence = Path(__file__).resolve().parents[1] / "docs/evidence"
        files = SimpleNamespace(
            corpus=evidence / "chronicle-waiting-entrant-corpus.json.gz",
            warmup=evidence / "chronicle-shadow-native-fee-warmup.json.gz",
            manifest=evidence / "chronicle-go-native-linux-arm64.json",
            release=evidence / "chronicle-waiting-linux-build.json",
            distributed_identity=evidence / "chronicle-go-native-linux-identities.json",
        )
        receipt = load(evidence / "chronicle-go-native-linux-arm64-parity.json.gz")
        args = (
            self.args[0],
            load(files.warmup),
            json.loads(files.release.read_text()),
            json.loads(files.manifest.read_text()),
            files,
        )
        summary = verify_linux(receipt, *args)
        self.assertEqual(summary["new_go_decisions"], 248)
        self.assertEqual(summary["disagreements_with_live_stock"], [])
        for field in ("image_id", "release_elf_sha256"):
            changed = copy.deepcopy(receipt)
            changed["runtime"][field] = "other"
            with self.assertRaisesRegex(ValueError, "registered Linux release"):
                verify_linux(changed, *args)
        changed = copy.deepcopy(receipt)
        linked = changed["runtime"]["linked"]
        linked["source"] = linked["distributed"]
        with self.assertRaisesRegex(ValueError, "native identity"):
            verify_linux(changed, *args)

    def test_private_candidate_summary_requires_identical_successful_platform_results(
        self,
    ):
        # Test the cross-platform comparison using a previously confirmed spend;
        # prospective parent, proof and fee validation has its own native tests.
        evidence = Path(__file__).resolve().parents[1] / "docs/evidence"
        fixture = load(evidence / "chronicle-shadow-native-fee-warmup.json.gz")
        parent, raw, _ = warmup(fixture)
        release = json.loads(
            (evidence / "chronicle-waiting-linux-build.json").read_text()
        )
        manifest = json.loads(
            (evidence / "chronicle-go-native-linux-arm64.json").read_text()
        )
        linux_raw = load(evidence / "chronicle-go-native-linux-arm64-parity.json.gz")
        mac_raw = json.loads(
            (evidence / "chronicle-go-mainnet-warmup.json").read_text()
        )
        captured_sha = "fixture"
        mac = {"evidence_sha256": captured_sha, "variants": {}}
        linux = {
            "variants": linux_raw["confirmed_warmup"],
            "runtime": linux_raw["runtime"],
        }
        for role, report in mac_raw.items():
            mac["variants"][role] = {"exit": 0, "report": report}
            for case in (report, linux["variants"][role]):
                case["kind"] = "first-prospective-unsigned-candidate"
                del case["altered_funding_amount"]
        candidate = {
            "raw_transactions": raw,
            "successor": {"hash": "fixture successor"},
        }
        checked_native = {
            "complete": True,
            "parent_height": parent,
            "parent_hash": "fixture parent",
            "candidate_transactions": 1,
            "candidate_fee_satoshis": 64,
            "cw144_bits": 1,
        }
        args = candidate, mac, self.args[1], linux, release, manifest, captured_sha
        with patch("linux_shadow_candidate.assess", return_value=checked_native):
            self.assertTrue(checked_summary(*args)["ordinary_validation_passed"])
            mac["variants"]["source"]["exit"] = 1
            self.assertFalse(checked_summary(*args)["ordinary_validation_passed"])
            mac["variants"]["source"]["exit"] = 0
            linux["variants"]["distributed"]["ordinary_rejections"].append(
                {"txid": "private payment"}
            )
            redacted = checked_summary(*args)
            self.assertFalse(redacted["variants"]["distributed"]["macos_matches_linux"])
            self.assertNotIn("private payment", json.dumps(redacted))

    def test_original_station_executable_follows_the_first_eligible_event_across_restarts(
        self,
    ):
        old, emitting, after = (c * 64 for c in "abc")
        decision = {
            "height": 100,
            "hash": "parent",
            "observed_at": 700,
            "unsigned_candidate": {"transactions": ["private"]},
        }
        rows = [
            {"event": "start", "executable_sha256": old},
            {**decision, "unsigned_candidate": None},
            {"event": "start", "executable_sha256": emitting},
            {"event": "tip", **decision},
            {"event": "start", "executable_sha256": after},
            {"event": "tip", **decision},
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "journal.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            self.assertEqual(origin_in_journal(path, decision), emitting)
            with self.assertRaisesRegex(ValueError, "first unsigned journal"):
                origin_in_journal(path, {**decision, "observed_at": 701})
