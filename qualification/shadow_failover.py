"""Restart a passive observer on its committed boundary, exercise SV-only body delivery and restore both paths."""

import argparse
import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from shadow import complete_lines


def direct(row):
    return row.get("event") == "tip" and row.get("receipt_trigger_hash") == row.get(
        "hash"
    )


def read_tips(path):
    if path.exists():
        with path.open("rb") as file:
            for line in complete_lines(file):
                row = json.loads(line)
                if direct(row):
                    yield row


def run(binary, journal, output, options):
    if (
        "--stratum-bind" in options
        or "--network" in options
        or "--shadow-log" in options
        or "--sv-bodies-only" in options
    ):
        raise ValueError(
            "the qualification driver owns network, shadow journal and body mode"
        )
    if output.exists():
        raise ValueError("use a fresh failover receipt for each attempt")
    args = [
        str(binary.resolve()),
        "--network",
        "mainnet",
        "--shadow-log",
        str(journal.resolve()),
        *options,
    ]
    before = None
    for row in read_tips(journal):
        before = row
    if before and not before.get("delivery_source", "").startswith(
        ("http://", "https://")
    ):
        before = None
    journal.touch(exist_ok=True)

    def append(event, **fields):
        with output.open("a") as f:
            f.write(
                json.dumps({"event": event, "unix_s": int(time.time()), **fields})
                + "\n"
            )
            f.flush()

            os.fsync(f.fileno())

    @contextlib.contextmanager
    def observer(sv_only):
        process = subprocess.Popen(args + (["--sv-bodies-only"] if sv_only else []))
        try:
            yield process
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            process.wait(timeout=45)

    def next_row(file, process, deadline=None):
        while True:
            if process.poll() is not None:
                raise RuntimeError(
                    "passive observer exited with status " + str(process.returncode)
                )
            position = file.tell()
            line = file.readline()
            if line.endswith(b"\n"):
                return json.loads(line)
            file.seek(position)
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.25)

    def terminate(signum, frame):
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, terminate)
    with journal.open("rb") as source:
        source.seek(0, 2)
        if before is None:
            with observer(False) as process:
                while before is None:
                    row = next_row(source, process)
                    if direct(row) and row.get("delivery_source", "").startswith(
                        ("http://", "https://")
                    ):
                        before = row
        append("restrict_body_transport", height=before["height"], hash=before["hash"])
        with observer(True) as process:
            deadline = None
            ahead_height = None
            restored = False
            while True:
                row = next_row(source, process, deadline)
                if row is None:
                    append(
                        "sv_body_unavailable",
                        reference_height=ahead_height,
                        restored=restored,
                    )
                    break
                if row.get("event") == "start":
                    restored = row.get("restored_tip") == before["hash"]
                elif direct(row):
                    matched = (
                        restored
                        and row["height"] == before["height"] + 1
                        and row["previous"] == before["hash"]
                        and row["delivery_source"].startswith("sv:")
                    )
                    append(
                        "sv_body_received",
                        height=row["height"],
                        hash=row["hash"],
                        restored=restored,
                        contiguous_failover=matched,
                    )
                    break
                elif (
                    row.get("event") == "reference_tip"
                    and row["height"] > before["height"]
                ):
                    deadline = deadline or time.monotonic() + 90
                    ahead_height = row["height"]
                    if ahead_height > before["height"] + 1:
                        append(
                            "sv_body_unavailable",
                            reference_height=row["height"],
                            restored=restored,
                        )
                        break
        append("restore_all_body_transports")
        with observer(False) as process:
            outcome = process.wait()
            if outcome:
                raise RuntimeError(
                    "passive observer exited with status " + str(outcome)
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("node_options", nargs=argparse.REMAINDER)
    a = parser.parse_args()
    opts = a.node_options[1:] if a.node_options[:1] == ["--"] else a.node_options
    try:
        run(a.binary, a.journal, a.receipt, opts)
    except KeyboardInterrupt:
        pass
