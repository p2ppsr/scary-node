"""Execute fresh native receiver sessions serially and retain all attempts and receipts."""

import argparse
import contextlib
import gzip
import fcntl
import json
import os
import sys
import time
from pathlib import Path
from cohort_estimate import analyze, count_session, read
from native_cohort import ROOT, run


@contextlib.contextmanager
def session_lock():
    # Independent cohort sessions on this workstation share a scheduling lock.
    with (ROOT / ".native/cohort-session.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def batch(directory, binary, phase, pilot):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".writer.lock").open("a") as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return execute(directory, binary, phase, pilot)


def execute(directory, binary, phase, pilot):
    start = 41001 if phase == "pilot" else 51001
    n = 16 if phase == "pilot" else read(pilot)["fresh_evaluation_sessions"]
    for seed in range(start, start + n):
        final = directory / f"{seed}.json.gz"
        if final.exists():
            count_session(read(final))
            continue
        partial = directory / f"{seed}.partial"
        if partial.exists():
            previous = read(partial)
            if previous.get("stage") == "fixture" and not previous["rows"]:
                failed = directory / "preflight"
                failed.mkdir(exist_ok=True)
                os.rename(partial, failed / f"{seed}-{time.time_ns()}.json")
            elif not previous["complete"]:
                raise RuntimeError(
                    f"measured partial retained at {partial}; review the missing observations"
                )
        if not partial.exists():
            with session_lock():
                run(partial, binary, seed, 32, 1200)
        record = read(partial)
        count_session(record)
        tmp = final.with_suffix(".next")
        tmp.write_bytes(gzip.compress(partial.read_bytes(), mtime=0))
        os.replace(tmp, final)
        partial.unlink()
        print(
            f"Completed session {seed} ({seed - start + 1}/{n})",
            file=sys.stderr,
            flush=True,
        )
    return analyze(directory, phase, n)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--phase", choices=["pilot", "evaluation"], required=True)
    p.add_argument("--pilot-summary", type=Path)
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/scary-node")
    a = p.parse_args()
    if a.phase == "evaluation" and not a.pilot_summary:
        p.error("evaluation requires the frozen pilot summary")
    print(json.dumps(batch(a.directory, a.binary, a.phase, a.pilot_summary), indent=2))
