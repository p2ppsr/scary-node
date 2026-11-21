"""Exercise an actual append between two reads at the old end of a live journal."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shadow import complete_lines
from shadow_export import export
from shadow_failover import read_tips
from shadow_first_candidate import journal_rows
from shadow_reader_supervisor import supervise


class JournalTailTests(unittest.TestCase):
    def test_suffix_visible_after_an_unterminated_read_waits_for_the_next_scan(self):
        previous = b'{"event":"tip","height":10}\n'
        whole = '{"event":"tip","height":11,"note":"café"}\n'.encode()
        for split in (len(whole) - 1, whole.index(b"\xc3") + 1, whole.index(b"11") + 1):
            with self.subTest(split=split), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "live.jsonl"
                path.write_bytes(previous + whole[:split])
                with path.open("rb") as reader, path.open("ab", buffering=0) as writer:

                    def extending(suffix=whole[split:]):
                        for line in reader:
                            if not line.endswith(b"\n"):
                                writer.write(suffix)
                            yield line

                    self.assertEqual(list(complete_lines(extending())), [previous])
                self.assertEqual(
                    [row["height"] for row in journal_rows(path)], [10, 11]
                )

    def test_partial_failover_and_peer_records_retry_but_malformed_complete_lines_fail(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "live.jsonl"
            actions = Path(tmp) / "failover.jsonl"
            journal.write_bytes(b"")
            actions.write_bytes(b'{"event":"one"}\n{"event":"two"')
            self.assertEqual(export(journal, actions)["failover"], [{"event": "one"}])
            with actions.open("ab") as f:
                f.write(b"}\n")
            self.assertEqual(len(export(journal, actions)["failover"]), 2)
            previous = b'{"event":"tip","hash":"a","receipt_trigger_hash":"a"}\n'
            journal.write_bytes(previous + b'{"event":')
            self.assertEqual(len(list(read_tips(journal))), 1)
            for bad in (b"\n", b"{broken}\n", b'{"note":"\xff"}\n'):
                journal.write_bytes(previous + bad)
                for read in (journal_rows, lambda p: list(read_tips(p))):
                    with self.assertRaises((json.JSONDecodeError, UnicodeDecodeError)):
                        read(journal)
                actions.write_bytes(b'{"event":"one"}\n' + bad)
                journal.write_bytes(b"")
                with self.assertRaises((json.JSONDecodeError, UnicodeDecodeError)):
                    export(journal, actions)

    def test_reader_nonzero_exit_recovers_but_an_invalid_complete_journal_holds(self):
        # An isolated child fails its first run before it can produce a result.
        child = (
            "from pathlib import Path\n"
            "import sys\n"
            "p=Path('attempted')\n"
            "if not p.exists(): p.touch();sys.exit(1)\n"
            "Path('finished').touch()\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for bad in (False, True):
                work = root / str(bad)
                work.mkdir()
                journal = work / "journal.jsonl"
                journal.write_bytes(
                    b'{"accepted":true}\n' + (b"bad\n" if bad else b'{"partial":')
                )
                command = [
                    sys.executable,
                    "-c",
                    "import os;os.chdir(" + repr(str(work)) + ")\n" + child,
                ]
                with patch(
                    "shadow_reader_supervisor.complete",
                    side_effect=lambda folder: (folder / "finished").exists(),
                ):
                    if bad:
                        with self.assertRaisesRegex(
                            ValueError, "malformed complete journal record 2"
                        ):
                            supervise(journal, work, command, retry_seconds=0)
                    else:
                        supervise(journal, work, command, retry_seconds=0)
                rows = list(
                    map(
                        json.loads,
                        (work / "reader-recoveries.jsonl").read_bytes().splitlines(),
                    )
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["exit_code"], 1)
                self.assertEqual(rows[0]["action"], "hold" if bad else "retry")
                self.assertEqual((work / "finished").exists(), not bad)
