"""Cache each of the first sixteen independently mined successors as its observed pair becomes available."""

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError

from shadow_checkpoint import atomic_write, checkpoint
from shadow_export import export
from shadow_inclusion import SAMPLE, compare, packed, read_block, require, run, selected


def path_for(cache, child):
    # selected() already authenticated the canonical displayed hash with the wire header.
    return cache / (child["hash"] + ".json.gz")


def attempt(cache, child, outcome):
    record = {
        "at": datetime.now(UTC).isoformat(),
        "height": child["height"],
        "hash": child["hash"],
        "outcome": outcome,
    }
    with (cache.parent / "attempts.jsonl").open("a") as log:
        log.write(json.dumps(record, separators=(",", ":")) + "\n")
        log.flush()
        os.fsync(log.fileno())


def cached_pairs(pairs, cache, held, *, read=read_block, now=time.monotonic):
    waiting = []
    for parent, child in pairs:
        key = child["hash"]
        dest = path_for(cache, child)
        if dest.exists():
            continue
        previous = held.get(key)
        if not previous or now() >= previous[0]:
            try:
                row = read(child)
                compare({"records": [parent, child]}, [row])
            except HTTPError as error:
                held[key] = (now() + 60, f"reference HTTP {error.code}")
            except (
                URLError,
                TimeoutError,
                ConnectionError,
                HTTPException,
                TypeError,
                ValueError,
                KeyError,
            ) as error:
                # A reference can publish pages after its header, or temporarily
                # select another branch. Keep its failed attempt out of the sample.
                held[key] = (
                    now() + 60,
                    str(error)
                    if isinstance(error, ValueError)
                    else f"reference readback failed ({type(error).__name__})",
                )
            else:
                atomic_write(dest, packed(row))
                held.pop(key, None)
                attempt(cache, child, "verified")
                continue
            attempt(cache, child, held[key][1])
        waiting.append({"height": child["height"], "hash": key, "reason": held[key][1]})
    return waiting


def gather(snapshot, work, held, *, read=read_block, now=time.monotonic):
    if not any(r.get("event", "tip") == "tip" for r in snapshot["records"]):
        return []
    pairs = selected(snapshot["records"])
    waiting = cached_pairs(pairs, work / "blocks", held, read=read, now=now)
    progress = {
        "scheduled_pairs": SAMPLE,
        "observed_pairs": len(pairs),
        "cached_successors": len(pairs) - len(waiting),
        "waiting": waiting,
    }
    output = (json.dumps(progress, indent=2) + "\n").encode()
    path = work / "progress.json"
    if not path.exists() or path.read_bytes() != output:
        atomic_write(path, output)
    return [c["hash"] for _, c in pairs] if len(pairs) == SAMPLE and not waiting else []


def finish(snapshot, work, planned):
    pairs = selected(snapshot["records"])
    if [child["hash"] for _, child in pairs] != planned or len(pairs) != SAMPLE:
        return None
    rows = [
        json.loads(gzip.decompress(path_for(work / "blocks", child).read_bytes()))
        for _, child in pairs
    ]
    expected = compare(snapshot, rows)
    snap = work / "checkpoint.json.gz"
    receipt = work / "successors.json.gz"
    checkpoint(snapshot, snap, False)
    checksum = hashlib.sha256(snap.read_bytes()).hexdigest()
    atomic_write(receipt, packed({"snapshot_sha256": checksum, "successors": rows}))
    summary = run("verify", snap, receipt)
    require(
        summary == expected and summary["sample_complete"],
        "final fixed sample could not be independently replayed",
    )
    atomic_write(work / "summary.json", (json.dumps(summary, indent=2) + "\n").encode())
    return summary


def follow(journal, failover, work):
    os.umask(0o077)
    work.mkdir(parents=True, exist_ok=True)
    (work / "blocks").mkdir(exist_ok=True)
    with (work / "reader.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done = work / "summary.json"
        if done.exists():
            summary = run(
                "verify", work / "checkpoint.json.gz", work / "successors.json.gz"
            )
            require(
                summary["sample_complete"] and summary == json.loads(done.read_text()),
                "saved inclusion result differs",
            )
            return summary
        held = {}
        while True:
            try:
                planned = gather(export(journal, failover), work, held)
                if planned:
                    # A new tip or reorg can arrive during paginated external reads.
                    result = finish(export(journal, failover), work, planned)
                    if result:
                        return result
            except (FileNotFoundError, PermissionError) as error:
                print(type(error).__name__, "local evidence unavailable", flush=True)
            time.sleep(30)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("journal", type=Path)
    p.add_argument("failover", type=Path)
    p.add_argument("work", type=Path)
    args = p.parse_args()
    print(json.dumps(follow(args.journal, args.failover, args.work), indent=2))
