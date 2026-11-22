import gzip
import json
import tempfile
import unittest
from pathlib import Path

from cohort_archive import canonical
from cohort_prefix import digest


class CohortPrefixTest(unittest.TestCase):
    def test_raw_and_sharded_carry_identical_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw, archive = root / "raw", root / "archive"
            raw.mkdir()
            archive.mkdir()
            first = 10**48
            spec = {
                "first_seed_decimal": str(first),
                "sealed_sessions": 2,
                "binary_sha256": "a" * 64,
            }
            rows = [
                {
                    "seed_decimal": str(first + i),
                    "binary_sha256": "a" * 64,
                    "complete": True,
                    "rows": [i],
                }
                for i in range(3)
            ]
            for i, row in enumerate(rows):
                (raw / f"{first + i}.json.gz").write_bytes(
                    gzip.compress(json.dumps(row, indent=2).encode())
                )
            for name, members in (
                ("sessions-00000.jsonl.gz", rows[:1]),
                ("sessions-00001.jsonl.gz", rows[1:]),
            ):
                (archive / name).write_bytes(
                    b"".join(gzip.compress(canonical(r)) for r in members)
                )
            original = digest(raw, spec)
            self.assertEqual(digest(archive, spec), original)
            rows[0]["rows"].append(7)
            (archive / "sessions-00000.jsonl.gz").write_bytes(
                gzip.compress(canonical(rows[0]))
            )
            self.assertNotEqual(digest(archive, spec), original)

    def test_requires_all_sealed_sessions_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            spec = {
                "first_seed_decimal": "41",
                "sealed_sessions": 2,
                "binary_sha256": "b" * 64,
            }
            row = {"seed": 41, "complete": True, "binary_sha256": "b" * 64}
            shard = directory / "sessions-41-42.jsonl.gz"
            shard.write_bytes(gzip.compress(canonical(row)))
            with self.assertRaises(ValueError):
                digest(directory, spec)
            for change in (
                {"seed": 43},
                {"seed": 42, "complete": False},
                {"seed": 42, "binary_sha256": "d" * 64},
            ):
                shard.write_bytes(
                    gzip.compress(canonical(row) + canonical({**row, **change}))
                )
                with self.assertRaisesRegex(ValueError, "session identity"):
                    digest(directory, spec)
