"""Extract stock processing durations for the exact measured regtest heights from retained container logs."""

import argparse
import json
from pathlib import Path
import re
import statistics
import subprocess

STAGES = {
    "validate_missing_transactions": re.compile(
        r"\[processTransactionsInLevels\] Processing (\d+) transactions at block height (\d+) DONE in (\S+)$"
    ),
    "validate_block": re.compile(
        r"\[ValidateBlock\]\[([0-9a-f]{64})\] validating block from .*? DONE in (\S+)$"
    ),
    "legacy_block": re.compile(
        r"\[HandleConvertedBlock\]\[([0-9a-f]{64}) \d+\] .*? DONE in (\S+)$"
    ),
    "persist_block": re.compile(
        r"\[BlockPersister\] Successfully processed block Block ([0-9a-f]{64}) .*? in (\S+)$"
    ),
}


def seconds(text):
    units = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "µs": 1e-6, "us": 1e-6, "ns": 1e-9}
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|µs|ns|us|h|m|s)", text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError("invalid stock timing: " + text)
    return sum(float(n) * units[u] for n, u in parts)


def collect(project, record):
    heights = {row["hash"]: row["height"] for row in record["rows"]}
    out = {}
    for node in ("tn1", "tn2"):
        observed = {stage: {} for stage in STAGES}
        with subprocess.Popen(
            ["docker", "logs", f"{project}-{node}-1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as log:
            for line in log.stdout:
                for stage, pattern in STAGES.items():
                    m = pattern.search(line.rstrip())
                    if not m:
                        continue
                    if stage == "validate_missing_transactions":
                        count, h, value = m.groups()
                        if (
                            int(count) != record["transactions_per_block"]
                            or int(h) not in heights.values()
                        ):
                            continue
                        height = int(h)
                    else:
                        key, value = m.groups()
                        if key not in heights:
                            continue
                        height = heights[key]
                    observed[stage].setdefault(str(height), []).append(seconds(value))
            if log.wait() != 0:
                raise RuntimeError("stock log retrieval failed")
        out[node] = {
            stage: {
                "covered_heights": len(rows),
                "median_seconds": statistics.median(values)
                if (values := [v for row in rows.values() for v in row])
                else None,
                "observations_seconds": rows,
            }
            for stage, rows in observed.items()
        }
    return {
        "workload_final_hash": record["rows"][-1]["hash"],
        "reported_stock_durations": out,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("project", choices=["scary-node-mixed", "scary-node-throughput"])
    p.add_argument("receipt", type=Path)
    args = p.parse_args()
    print(
        json.dumps(
            collect(args.project, json.loads(args.receipt.read_text())), indent=2
        )
    )
