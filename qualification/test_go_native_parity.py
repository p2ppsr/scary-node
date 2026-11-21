"""A shared release version cannot substitute for a native archive's revision or case outcome."""

import copy
import json
import unittest
from pathlib import Path

from go_native_parity import sha, verify
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
