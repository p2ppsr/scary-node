"""A shared release version cannot substitute for a native archive's revision or case outcome."""

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from go_native_parity import sha, verify
from linux_go_parity import verify_linux
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
