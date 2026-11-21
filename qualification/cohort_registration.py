"""Preregister a distinct release and run both fixed phases using the existing native cohort."""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from cohort_archive import EVIDENCE, ROOT, check, sha
from cohort_estimate import read
from sv_reference import IMAGE

# Include the actual experiment drivers, transaction/candidate helpers and estimator.
HARNESS = [
    "qualification/cohort_registration.py",
    "qualification/cohort_archive.py",
    "qualification/cohort_estimate.py",
    "qualification/run_cohort.py",
    "qualification/native_cohort.py",
    "qualification/scary_dar.py",
    "qualification/chronicle.py",
    "qualification/sv_dar.py",
    "qualification/sv_reference.py",
    "regtest/race_work.py",
    "regtest/prove.py",
    "regtest/verify_receipt.py",
]
STRIDE = 1 << 32


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def published_design(namespace, binary, release):
    check(1 <= namespace < 2**21, "choose an unused positive namespace below 2^21")
    revision = git("rev-parse", "--verify", release + "^{commit}").decode().strip()
    harness = git("rev-parse", "HEAD").decode().strip()
    hashes = {f: sha(ROOT / f) for f in HARNESS}
    for path, digest in hashes.items():
        check(
            hashlib.sha256(git("show", f"{harness}:{path}")).hexdigest() == digest,
            f"commit the exact measurement harness before registering: {path}",
        )
    base = namespace * STRIDE
    return {
        "study": f"release-{namespace}",
        "namespace": namespace,
        "release_commit": revision,
        "binary_sha256": sha(binary),
        "measurement_commit": harness,
        "measurement_file_sha256": hashes,
        "sv_image": IMAGE,
        "python_minor": list(sys.version_info[:2]),
        "first_seeds": {"pilot": base + 1, "evaluation": base + 17},
        "pilot_sessions": 16,
        "opportunities_per_session": 32,
        "offer_delay_ms": 1200,
        "target_half_width": 0.01,
        "evaluation_sizing_rule": "max(16, ceil((t(0.975,15) * pilot paired session influence SD / 0.01)^2))",
    }


def register(namespace, binary, release, ledger):
    design = published_design(namespace, binary, release)
    dest = ledger / f"chronicle-release-{namespace}-registration.json"
    ledger.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        check(
            read(dest)["design"] == design,
            "namespace already reserved for a different release or experiment",
        )
        return dest
    value = {
        "registered_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "design": design,
    }
    # A hard link publishes one complete registration without replacing a simultaneous writer.
    with tempfile.NamedTemporaryFile(mode="w", dir=ledger) as f:
        json.dump(value, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        try:
            os.link(f.name, dest)
        except FileExistsError:
            check(
                read(dest)["design"] == design,
                "namespace reserved concurrently for a different release",
            )
        fd = os.open(ledger, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return dest


def run(registration, directory, binary):
    import fcntl

    from cohort_estimate import STUDIES
    from run_cohort import batch, publish_summary

    d = read(registration)["design"]
    ns = d["namespace"]
    base = ns * STRIDE
    name = d["study"]
    check(
        1 <= ns < 2**21
        and name == f"release-{ns}"
        and d["first_seeds"] == {"pilot": base + 1, "evaluation": base + 17},
        "invalid seed namespace",
    )
    check(
        d["binary_sha256"] == sha(binary)
        and d["sv_image"] == IMAGE
        and d["python_minor"] == list(sys.version_info[:2]),
        "registered binary, reference or Python differs",
    )
    check(
        d["measurement_file_sha256"] == {f: sha(ROOT / f) for f in HARNESS},
        "measurement code changed; use the registered checkout",
    )
    check(
        d["pilot_sessions"] == 16
        and d["opportunities_per_session"] == 32
        and d["target_half_width"] == 0.01
        and d["offer_delay_ms"] == 1200,
        "registered experiment differs from runner",
    )
    STUDIES[name] = d["first_seeds"]
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".study.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        local = directory / "registration.json"
        if not local.exists():
            check(
                not any(f.name != ".study.lock" for f in directory.iterdir()),
                "existing data have no preregistration",
            )
            publish_summary(local, read(registration))
        check(read(local) == read(registration), "working study registration differs")
        pilot = directory / "pilot-summary.json"
        for phase in ("pilot", "evaluation"):
            if phase == "evaluation":
                check(
                    read(pilot)["fresh_evaluation_sessions"] < STRIDE - 17,
                    "planned evaluation falls outside the registered namespace",
                )
            out = batch(
                directory / phase, binary, phase, pilot, name, d["binary_sha256"]
            )
            publish_summary(directory / f"{phase}-summary.json", out)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    subs = p.add_subparsers(dest="action", required=True)
    r = subs.add_parser("register")
    r.add_argument("--namespace", type=int, required=True)
    r.add_argument("--release-commit", required=True)
    r.add_argument("--ledger", type=Path, default=EVIDENCE)
    u = subs.add_parser("run")
    u.add_argument("registration", type=Path)
    u.add_argument("directory", type=Path)
    for v in [r, u]:
        v.add_argument("--binary", type=Path, required=True)
    a = p.parse_args()
    if a.action == "register":
        print(register(a.namespace, a.binary, a.release_commit, a.ledger))
    else:
        run(a.registration, a.directory, a.binary)
