"""Verify a sealed prefix of raw or sharded cohort receipts without estimating outcomes."""

import argparse
import gzip
import hashlib
import json
from itertools import islice
from pathlib import Path

from cohort_archive import canonical, check


def records(directory, first, count):
    if (directory / f"{first}.json.gz").is_file():
        for seed in range(first, first + count):
            with gzip.open(directory / f"{seed}.json.gz", "rb") as f:
                yield json.load(f)
        return
    shards = sorted(
        directory.glob("sessions-*.jsonl.gz"),
        key=lambda path: int(path.name.split("-")[1].split(".")[0]),
    )
    for shard in shards:
        with gzip.open(shard, "rb") as f:
            for line in f:
                yield json.loads(line)


def digest(directory, prefix):
    first = int(prefix["first_seed_decimal"])
    count = prefix["sealed_sessions"]
    check(type(count) is int and count > 0, "empty or invalid session count")
    fingerprint = hashlib.sha256()
    rows = records(directory, first, count)
    try:
        for seed, row in zip(
            range(first, first + count), islice(rows, count), strict=True
        ):
            check(
                int(row.get("seed_decimal", row.get("seed"))) == seed
                and row["binary_sha256"] == prefix["binary_sha256"]
                and row["complete"] is True,
                "session identity, executable or closure differs",
            )
            fingerprint.update(canonical(row))
    finally:
        rows.close()
    return fingerprint.hexdigest()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("study", choices=("original", "immediate", "matched"))
    p.add_argument("directory", type=Path)
    a = p.parse_args()
    prefix = json.loads(a.receipt.read_bytes())["studies"][a.study]
    check(
        digest(a.directory, prefix) == prefix["canonical_receipts_sha256"],
        "sealed cohort prefix differs",
    )
    print(
        f"Verified {prefix['sealed_sessions']} original sealed sessions for {a.study}."
    )
