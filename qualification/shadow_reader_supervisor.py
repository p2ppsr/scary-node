"""Supervise the finite first-candidate reader, preserving its failure history and private spool."""

import argparse
import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

from shadow import complete_lines
from shadow_checkpoint import atomic_write
from shadow_first_candidate import assess, load


def complete(work):
    summary = work / "summary.json"
    if not summary.exists():
        return False
    result = json.loads(summary.read_bytes())
    if result.get("complete") is not True:
        return False
    if assess(load(work / "first.json.gz")) != result:
        raise ValueError(
            "terminal summary differs from its original prospective evidence"
        )
    return True


def prefix_length(journal):
    count = 0
    with journal.open("rb") as source:
        for line in complete_lines(source):
            try:
                json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"malformed complete journal record {count + 1}"
                ) from exc
            count += 1
    return count


def outcome(work, **event):
    with (work / "reader-recoveries.jsonl").open("a") as log:
        log.write(json.dumps({"at": int(time.time()), **event}) + "\n")
        log.flush()
        os.fsync(log.fileno())


def supervise(journal, work, command, retry_seconds=1):
    with (work / "watch.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        delay = retry_seconds
        while not complete(work):
            with (work / "reader.log").open("ab", buffering=0) as log:
                child = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            atomic_write(work / "reader.pid", f"{child.pid}\n".encode())
            code = child.wait()
            if code == 0 and complete(work):
                return
            while True:
                try:
                    records = prefix_length(journal)
                    break
                except ValueError as error:
                    outcome(work, exit_code=code, action="hold", diagnostic=str(error))
                    raise
                except OSError as error:
                    outcome(
                        work,
                        exit_code=code,
                        action="await_readable_prefix",
                        diagnostic=type(error).__name__,
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
            outcome(work, exit_code=code, action="retry", complete_records=records)
            time.sleep(delay)
            delay = min(delay * 2, 60)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("journal", type=Path)
    p.add_argument("work", type=Path)
    p.add_argument(
        "argv",
        type=Path,
        help="private JSON argument array for the first-candidate follow process",
    )
    a = p.parse_args()
    command = json.loads(a.argv.read_bytes())
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(v, str) for v in command)
    ):
        p.error("reader argv must be a nonempty array of strings")
    os.umask(0o077)
    a.work.mkdir(mode=0o700, parents=True, exist_ok=True)
    supervise(a.journal, a.work, command)
