"""Time bounded notice batches from an existing private observer log without contacting the node."""

import argparse
import itertools
import json
import os
import resource
import statistics
import sys
import time
from pathlib import Path

from shadow_subtree_queue import FRAME, START, check

COUNT = 512
PERIOD = 0.020


def summary(r):
    batches = r["batches"]
    rows = [(lo, hi, q) for lo, hi, flags in batches for q in flags]
    check(
        len(rows) == COUNT and all(0 <= lo <= hi and q in (0, 1) for lo, hi, q in rows),
        "incomplete bounded notice trace",
    )
    check(
        all(a[1] <= b[0] for a, b in itertools.pairwise(batches)),
        "overlapping poll windows",
    )
    following = [
        (max(0, after[0] - before[1]), after[1] - before[0])
        for before, after in itertools.pairwise(rows)
        if not before[2]
    ]
    widths = sorted(hi - lo for lo, hi, _ in batches)
    return {
        "occupied_window_us": {
            k: widths[int((len(widths) - 1) * p)]
            for k, p in (("median", 0.5), ("p95", 0.95), ("maximum", 1))
        },
        "announcements": len(rows),
        "queue_full": sum(q for _, _, q in rows),
        "poll_batches": len(batches),
        "largest_single_read_batch": max(len(flags) for _, _, flags in batches),
        "nonfull_followed_by_notice": len(following),
        "median_nonfull_to_next_interval_us": [
            statistics.median(a for a, b in following),
            statistics.median(b for a, b in following),
        ]
        if following
        else None,
        "elapsed_sec": round(r["elapsed_us"] / 1e6, 3),
        "cpu_sec": round(r["cpu_us"] / 1e6, 3),
        "peak_rss_bytes": r["peak_rss_bytes"],
        "bytes_read": r["bytes_read"],
    }


def check_log(path, receipt):
    offset = receipt["start_offset"]
    with path.open("rb") as source:
        before = source.read(offset)
        start = before.rfind(START)
        check(start >= 0, "process start absent from original log")
        prefix = sum(m[1] is not None for m in FRAME.finditer(before[start:]))
        capture = source.read(receipt["bytes_read"])
    flags = [int(m[3] == b"true") for m in FRAME.finditer(capture) if m[1]][:COUNT]
    check(
        flags == [q for lo, hi, f in receipt["batches"] for q in f],
        "timed flags differ from original private log",
    )
    return {"first_notice": prefix + 1, "last_notice": prefix + COUNT}


def watch(path):
    batches = []
    rest = b""
    count = 0
    total = 0
    polls = 0
    start = time.monotonic_ns()
    cpu = time.process_time_ns()
    last = 0
    with path.open("rb", buffering=0) as f:
        start_offset = f.seek(0, os.SEEK_END)
        while count < COUNT:
            time.sleep(PERIOD)
            # A complete record became visible sometime after the prior EOF and
            # before this read. No application or network receipt clock is implied.
            payload = f.read(256 * 1024)
            now = (time.monotonic_ns() - start) // 1000
            check(now <= 3600_000_000, "one-hour observation limit")
            check(len(payload) < 256 * 1024, "tail fell behind the bounded read window")
            total += len(payload)
            polls += 1
            lines = (rest + payload).split(b"\n")
            rest = lines.pop()
            flags = []
            for line in lines:
                check(START not in line, "observer restarted during bounded read")
                frames = list(FRAME.finditer(line))
                check(
                    line.count(b'"event":"subtree_notice"')
                    == sum(m[1] is not None for m in frames),
                    "unrecognized subtree event",
                )
                for m in frames:
                    if m[1] and count < COUNT:
                        flags.append(int(m[3] == b"true"))
                        count += 1
            if flags:
                batches.append([last, now, flags])
            last = now
    return {
        "started_utc_unix_us": time.time_ns() // 1000 - now,
        "start_offset": start_offset,
        "poll_interval_ms": 20,
        "batches": batches,
        "polls": polls,
        "elapsed_us": now,
        "cpu_us": (time.process_time_ns() - cpu) // 1000,
        "bytes_read": total,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if sys.platform == "darwin" else 1024),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["capture", "verify"])
    p.add_argument("receipt", type=Path)
    p.add_argument("--log", type=Path)
    a = p.parse_args()
    if a.mode == "capture":
        check(
            a.log and not a.receipt.exists(),
            "capture needs an observer log and a fresh receipt",
        )
        r = watch(a.log)
        summary(r)
        with a.receipt.open("x") as f:
            print(json.dumps(r, separators=(",", ":")), file=f)
    else:
        r = json.loads(a.receipt.read_bytes())
    result = summary(r)
    if a.mode == "verify" and a.log:
        result.update(check_log(a.log, r))
    print(json.dumps(result, indent=2))
