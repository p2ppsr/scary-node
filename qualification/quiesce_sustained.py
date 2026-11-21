"""Pause the completed direct-wallet regtest and record its stock queue at the cohort handoff."""

import argparse
import gzip
import json
import subprocess
import time
from pathlib import Path

from sustained_stock import snapshot
from verify_sustained_native import stock_activity, verify

PROJECT = "scary-node-throughput"
SERVICES = {
    "svref",
    "tn1",
    "tn2",
    "scary-node",
    "as1",
    "as2",
    "ingress",
    "wallet-ingress",
}


def quiesce(directory):
    from mixed_storage_trial import docker, rpc, sv
    from prove import request

    output = directory / "quiesced.json"
    if output.exists():
        raise ValueError("handoff already captured")
    receipt = directory / "receipt.json.gz"
    while not receipt.exists():
        time.sleep(0.2)
    first = time.time_ns()
    with gzip.open(receipt, "rt") as f:
        result = json.load(f)
    if result["registration"] != "11e60074fbdde9587e1652d4de272a24bd4ac68b":
        raise ValueError("handoff is only for the registered direct-wallet experiment")
    summary = verify(result)
    if not result["completion"]["primary_complete"] and not (
        result["completion"]["resource_stop"] or result["completion"]["audit_failure"]
    ):
        raise ValueError(
            "direct-wallet cohort is still incomplete without terminal closure"
        )
    services = docker(
        "ps",
        "--filter",
        f"label=com.docker.compose.project={PROJECT}",
        "--format",
        '{{.Label "com.docker.compose.service"}} {{.ID}}',
    )
    enrolled = dict(line.split() for line in services.splitlines())
    if set(enrolled) != SERVICES:
        raise ValueError(
            "expected precisely the eight independent direct-wallet containers"
        )
    final = snapshot(sv)
    tips = {}
    for name, observe in [
        ("sv", lambda: sv("getblockchaininfo")),
        ("tn1", lambda: rpc((21441, 21451), "getblockchaininfo")),
        ("tn2", lambda: rpc((21442, 21452), "getblockchaininfo")),
    ]:
        info = observe()
        tips[name] = {
            "height": info["blocks"],
            "hash": info["bestblockhash"],
            "wall_ns": time.time_ns(),
        }
    mining = json.loads(request("http://127.0.0.1:21480/status"))["mining"]
    tips["scary"] = {
        "height": mining["height"],
        "hash": mining["tip_hash"],
        "wall_ns": time.time_ns(),
    }
    suffix = [
        {
            k: obs[k]
            for k in (
                "height",
                "hash",
                "noncoinbase_transactions",
                "known_inputs",
                "unknown_inputs",
                "script_checks_passed",
            )
        }
        for obs in mining["validation_window"]
        if obs["height"] > result["completion"]["final_height"]
    ]
    started = time.time_ns()
    subprocess.run(
        ["docker", "pause", *enrolled.values()], check=True, stdout=subprocess.DEVNULL
    )
    paused = {
        name: docker("inspect", "--format", "{{.State.Paused}}", container) == "true"
        for name, container in enrolled.items()
    }
    if not all(paused.values()):
        raise RuntimeError("incomplete measured-regtest quiescence")
    record = {
        "registration": result["registration"],
        "project": PROJECT,
        "receipt_seen_wall_ns": first,
        "first_scheduled_offer_wall_ns": result["start_wall_ns"],
        "observation_duration_ns": result["duration_ns"],
        "audited_blocks": len(result["blocks"]),
        "confirmed_payments": summary["distinct_confirmed"],
        "post_window_stock_observation": final,
        "post_window_selected_tips": tips,
        "post_window_entrant_observed_above_audited_prefix": suffix,
        "pause_started_wall_ns": started,
        "pause_verified_wall_ns": time.time_ns(),
        "paused_services": paused,
    }
    tmp = directory / "quiesced.next"
    tmp.write_text(json.dumps(record, indent=2) + "\n")
    tmp.replace(output)
    return record


def verify_handoff(receipt, stock_log, handoff):
    def read(path):
        return gzip.open(path, "rt") if path.suffix == ".gz" else path.open()

    with read(receipt) as f:
        result = json.load(f)
    with read(handoff) as f:
        marker = json.load(f)
    with read(stock_log) as f:
        rows = [json.loads(line) for line in f]
    summary = verify(result)
    start = result["start_wall_ns"]
    final = marker["post_window_stock_observation"]
    times = [
        start + result["duration_ns"],
        marker["receipt_seen_wall_ns"],
        final["wall_ns"],
        final["finished_wall_ns"],
        marker["pause_started_wall_ns"],
        marker["pause_verified_wall_ns"],
    ]
    if times != sorted(times):
        raise ValueError("handoff precedes the primary completion boundary")
    if not (
        marker["registration"]
        == result["registration"]
        == "11e60074fbdde9587e1652d4de272a24bd4ac68b"
        and marker["project"] == PROJECT
        and marker["paused_services"] == dict.fromkeys(SERVICES, True)
        and marker["confirmed_payments"] == summary["distinct_confirmed"]
        and marker["audited_blocks"] == len(result["blocks"])
        and marker["observation_duration_ns"] == result["duration_ns"]
        and marker["first_scheduled_offer_wall_ns"] == start
    ):
        raise ValueError("queued stock work or closed primary identity differs")
    if not rows or any(b["wall_ns"] < a["wall_ns"] for a, b in zip(rows, rows[1:])):
        raise ValueError("stock gauges missing or out of order")
    return {
        "stock_samples": len(rows),
        "sampled_stock": stock_activity(rows, start),
        "final_stock_before_pause": stock_activity(
            [{**final, "elapsed_ns": final["wall_ns"] - start}], start
        ),
        "duration_through_primary_closure_s": result["duration_ns"] / 1e9,
        "seconds_between_closure_and_verified_pause": (times[-1] - times[0]) / 1e9,
        "paused_services": sorted(SERVICES),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        type=Path,
        help="live run directory, or a closed receipt for offline verification",
    )
    parser.add_argument(
        "--stock-log",
        type=Path,
        help="independent stock JSONL or gzip for offline verification",
    )
    parser.add_argument(
        "--handoff", type=Path, help="closed handoff JSON for offline verification"
    )
    args = parser.parse_args()
    if args.stock_log or args.handoff:
        if not (args.stock_log and args.handoff):
            parser.error("offline verification requires both --stock-log and --handoff")
        result = verify_handoff(args.path, args.stock_log, args.handoff)
    else:
        result = quiesce(args.path)
    print(json.dumps(result, indent=2))
