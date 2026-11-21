"""Record low-frequency stock intake, mempool and orphan gauges for the isolated sustained trials."""

import argparse
import json
import subprocess
import time
from pathlib import Path


def snapshot(sv):
    started = time.time_ns()
    row = {}
    for method in ("getmempoolinfo", "getblockchainactivity", "getorphaninfo"):
        try:
            row[method] = sv(method)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            row[method] = {"error": type(exc).__name__}
    try:
        groups = {}
        for peer in sv("getpeerinfo"):
            agent = peer["subver"]
            group = next(
                (g for g in ("wallet", "scary-node", "teranode") if g in agent), "other"
            )
            stats = groups.setdefault(group, {"connections": 0})
            stats["connections"] += 1
            for direction in ("recv", "sent"):
                amounts = peer.get(f"bytes{direction}_per_msg", {})
                for msg in ("inv", "getdata", "tx", "reject"):
                    key = f"{direction}_{msg}_bytes"
                    stats[key] = stats.get(key, 0) + amounts.get(msg, 0)
        row["peers"] = groups
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        row["peers"] = {"error": type(exc).__name__}
    return {"wall_ns": started, "finished_wall_ns": time.time_ns(), **row}


def capture(directory, sv, stop=None):
    attempt = directory / "attempt.json"
    while not attempt.exists():
        time.sleep(0.2)
    base = json.loads(attempt.read_text())["start_wall_ns"]
    with (directory / "stock.jsonl").open("x", buffering=1) as log:
        while not (directory / "receipt.json.gz").exists() and not (
            stop and stop.is_set()
        ):
            observed = snapshot(sv)
            observed["elapsed_ns"] = observed["wall_ns"] - base
            log.write(json.dumps(observed, separators=(",", ":")) + "\n")
            if observed["elapsed_ns"] > 2_112_000_000_000:
                break
            due = (observed["elapsed_ns"] // 30_000_000_000 + 1) * 30_000_000_000 + base
            delay = max(0, (due - time.time_ns()) / 1e9)
            if stop:
                stop.wait(delay)
            else:
                time.sleep(delay)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    from mixed_storage_trial import sv

    capture(args.directory, sv)
