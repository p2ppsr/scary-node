"""Check fixed-N blinding, retry accounting, interval replay and immutable registration."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cohort_archive as archive
from cohort_estimate import read
from cohort_registration import register


class CohortArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = archive.EVIDENCE / "chronicle-native-pilot"
        self.registration = archive.original_registration()
        self.design = self.registration["design"]

    def test_independent_replay_and_lossless_archive(self):
        destination = self.work / "archive"
        m = archive.export(self.source, destination, self.registration, "pilot", None)
        expected = read(archive.EVIDENCE / "chronicle-native-sizing.json")
        self.assertEqual(m["fresh_evaluation_sessions"], 2263)
        self.assertEqual(len(m["setup_retries"]), 1)
        self.assertEqual(m["scheduled_parents"], 512)
        for a in archive.ARMS:
            got = m["arms"][a]
            want = expected["arms"][a]
            self.assertEqual(
                got["conditional_stale"]["confidence_interval_95"],
                want["confidence_interval_95"],
            )
            for i in range(2):
                self.assertEqual(
                    sum(got["unready_reasons"][i].values()),
                    want["roles"][i]["scheduled"] - want["roles"][i]["ready"],
                )
                for k, v in want["roles"][i].items():
                    self.assertEqual(got["roles"][i][k], v)
        self.assertEqual(
            m,
            archive.export(self.source, destination, self.registration, "pilot", None),
        )
        altered = copy.deepcopy(m)
        altered["arms"][archive.ARMS[0]]["conditional_stale"]["difference"] = 0.0
        (destination / "manifest.json").write_text(json.dumps(altered))
        with self.assertRaisesRegex(ValueError, "uncertainty"):
            archive.verify(destination)

    def test_no_outcome_access_before_fixed_n(self):
        (self.work / "41001.json.gz").write_bytes(b"unread outcome")
        with self.assertRaisesRegex(ValueError, "fixed phase is open"):
            archive.export(
                self.work, self.work / "target", self.registration, "pilot", None
            )

    def test_interrupted_rows_and_unclassified_attempts(self):
        for src in self.source.glob("*.json.gz"):
            os.link(src, self.work / src.name)
        (self.work / "41008.partial").write_text("outcome should stay unread")
        with self.assertRaisesRegex(ValueError, "unaccounted attempt"):
            archive.closed_files(self.work, 41001, 16)
        attempt = read(self.source / "41001.json.gz")
        with self.assertRaisesRegex(ValueError, "measured interruption"):
            archive.inspect_preflight(
                {"name": "41001.json", "receipt": attempt}, self.design, 41001, 16
            )

    def test_schedule_and_external_identity(self):
        data = read(self.source / "41002.json.gz")
        data["physical_swap"] = not data["physical_swap"]
        with self.assertRaisesRegex(ValueError, "seeded schedule"):
            archive.validate_session(data, self.design, 41002)
        orphan = copy.deepcopy(self.registration)
        orphan["design"]["binary_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "registration differs"):
            archive.bind_registration(orphan)

    def test_evaluation_cannot_substitute_its_own_sample_size(self):
        sizing = read(archive.EVIDENCE / "chronicle-native-sizing.json")
        sizing["arms"][archive.ARMS[0]]["session_influence_sd"] = 0.0
        sizing["fresh_evaluation_sessions"] = 16
        self.assertEqual(archive.phase_size(self.design, "evaluation", sizing), 16)
        with self.assertRaisesRegex(ValueError, "independent completed pilot"):
            archive.bind_pilot(
                self.design, "evaluation", sizing, None, self.registration
            )

    def test_namespace_is_reserved_once(self):
        with patch(
            "cohort_registration.published_design",
            return_value={"study": "release-9", "binary_sha256": "a" * 64},
        ):
            first = register(9, None, None, self.work)
            original = first.read_bytes()
            self.assertEqual(register(9, None, None, self.work).read_bytes(), original)
        with (
            patch(
                "cohort_registration.published_design",
                return_value={"study": "release-9", "binary_sha256": "b" * 64},
            ),
            self.assertRaisesRegex(ValueError, "already reserved"),
        ):
            register(9, None, None, self.work)
        self.assertEqual(first.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
