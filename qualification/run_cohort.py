"""Execute fresh native receiver sessions serially and retain all attempts and receipts."""

import argparse
import contextlib
import datetime
import gzip
import hashlib
import fcntl
import json
import os
import sys
import time
from pathlib import Path
from cohort_estimate import STUDIES, analyze, count_session, read
from native_cohort import ROOT, run


@contextlib.contextmanager
def session_lock():
    # Independent cohort sessions on this workstation share a scheduling lock.
    with (ROOT / ".native/cohort-session.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def batch(directory, binary, phase, pilot, study="original", expected_binary=None):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".writer.lock").open("a") as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return execute(directory, binary, phase, pilot, study, expected_binary)


def execute(directory, binary, phase, pilot, study, expected_binary):
    start = STUDIES[study][phase]
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if expected_binary and digest != expected_binary:
        raise ValueError("binary differs from the registered executable")
    n = 16
    if phase == "evaluation":
        sizing = read(pilot)
        if (
            sizing.get("study", "original") != study
            or sizing["phase"] != "pilot"
            or sizing["binary_sha256"] != digest
            or sizing["sessions"] != 16
        ):
            raise ValueError(
                "evaluation requires this executable's own 16-session sizing pilot"
            )
        n = sizing["fresh_evaluation_sessions"]

    def validate(receipt, seed):
        if receipt["binary_sha256"] != digest or receipt["seed"] != seed:
            raise ValueError("saved receipt belongs to a different session or binary")
        return count_session(receipt)

    for seed in range(start, start + n):
        final = directory / f"{seed}.json.gz"
        if final.exists():
            validate(read(final), seed)
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
        validate(record, seed)
        tmp = final.with_suffix(".next")
        tmp.write_bytes(gzip.compress(partial.read_bytes(), mtime=0))
        os.replace(tmp, final)
        partial.unlink()
        print(
            f"Completed session {seed} ({seed - start + 1}/{n})",
            file=sys.stderr,
            flush=True,
        )
    return analyze(directory, phase, n, study, digest)


def both_phases(directory, binary, study, expected_binary):
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if expected_binary is not None and digest != expected_binary:
        raise ValueError("binary differs from registered executable")
    pinned = {
        "study": study,
        "binary_sha256": digest,
        "pilot_sessions": 16,
        "first_seeds": STUDIES[study],
        "opportunities_per_session": 32,
        "offer_delay_ms": 1200,
        "target_half_width": 0.01,
    }
    registration = directory / "registration.json"
    with (directory / ".study.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if registration.exists():
            if read(registration)["design"] != pinned:
                raise ValueError("existing registration differs from requested study")
        else:
            if any(directory.glob("*/*.json.gz")) or any(directory.glob("*/*.partial")):
                raise ValueError("study data already exist without a registration")
            publish_summary(
                registration,
                {
                    "registered_utc": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "design": pinned,
                },
            )
        for phase in ("pilot", "evaluation"):
            target = directory / (phase + "-summary.json")
            summary = batch(
                directory / phase,
                binary,
                phase,
                directory / "pilot-summary.json",
                study,
                digest,
            )
            publish_summary(target, summary)
    return summary


def publish_summary(path, summary):
    temp = path.with_suffix(".next")
    with temp.open("w") as stream:
        json.dump(summary, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--phase", choices=["pilot", "evaluation", "both"], required=True)
    p.add_argument("--study", choices=STUDIES, default="original")
    p.add_argument("--expect-sha256", help="require this measured executable")
    p.add_argument("--pilot-summary", type=Path)
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/scary-node")
    a = p.parse_args()
    if a.phase == "evaluation" and not a.pilot_summary:
        p.error("evaluation requires the frozen pilot summary")
    if a.phase == "both":
        result = both_phases(a.directory, a.binary, a.study, a.expect_sha256)
    else:
        result = batch(
            a.directory, a.binary, a.phase, a.pilot_summary, a.study, a.expect_sha256
        )
    print(json.dumps(result, indent=2))
