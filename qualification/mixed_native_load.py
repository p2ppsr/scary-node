"""Send signed regtest blocks to one SV ingress and measure native adoption on two Teranodes and Scary Node."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
from mixed_native import NODES, assert_tip, block, mixed_sv, rpc, status, wait
from native_workload import ToHex, signed_bodies, signed_funding, solve, wallet_coins
from prove import compose
from run_cohort import session_lock


def sv(method, *params):
    raw = mixed_sv(method, *params)
    if not raw:
        return None
    if raw[0] in "{[":
        return json.loads(raw)
    return raw


def all_active(h):
    return all(assert_tip(n, h) for n in NODES) and status()["mining"]["tip_hash"] == h


def run(directory, rounds, fanout):
    assert (
        status()["network"] == "regtest"
        and sv("getblockchaininfo")["chain"] == "regtest"
    )
    assert any("teranode" in p["subver"].lower() for p in sv("getpeerinfo"))
    # The source wallet must have enough mature funds for all funded outputs.
    for _ in range(103):
        if wallet_coins(sv, fanout)[0]:
            break
        h = sv("generate", 1)[0]
        wait(
            "maturing SV wallet funds on the three receiving stores",
            lambda: all_active(h),
        )
    base = sv("getblock", sv("getbestblockhash"))
    case, key, funding = signed_funding(sv, fanout)
    height, stamp = base["height"] + 1, base["time"] + 1
    first = solve(base["hash"], height, stamp, [funding])
    assert sv("submitblock", ToHex(first)) is None
    wait("fully observed funding", lambda: all_active(first.hash))
    bodies = signed_bodies(
        directory, case, key, funding, fanout, rounds, first.hash, height + 1, stamp + 1
    )

    rows = []
    start = time.monotonic_ns()
    final = bodies[-1][0]
    final_observations = {}

    def sample_receivers():
        tips = {}
        for label, node in zip(("tn1", "tn2"), NODES):
            state = rpc(node, "getblockchaininfo")
            tips[label] = (
                state["blocks"],
                state["bestblockhash"],
                time.monotonic_ns() - start,
            )
        mining = status()["mining"]
        tips["scary_node"] = (
            mining["height"],
            mining["tip_hash"],
            time.monotonic_ns() - start,
        )
        for label, (h, observed, t) in tips.items():
            if observed == final and label not in final_observations:
                final_observations[label] = {
                    "height": h,
                    "hash": observed,
                    "first_observed_ns": t,
                }
        return tips

    for i, (h, path) in enumerate(bodies):
        raw = path.read_bytes()
        offered = time.monotonic_ns()
        assert sv("submitblock", raw.hex()) is None
        rows.append(
            {
                "height": height + 1 + i,
                "hash": h,
                "header_hex": raw[:80].hex(),
                "bytes": len(raw),
                "wire_sha256": hashlib.sha256(raw).hexdigest(),
                "offered_ns": offered - start,
                "source_accepted_ns": time.monotonic_ns() - start,
            }
        )
        # Limit outstanding work without requiring that each receiver poll sees
        # every transient tip. Audit each selected active ancestor afterwards.
        if i >= 3:
            floor = height + i - 2
            polls = []

            def within_window():
                heights = {name: tip[0] for name, tip in sample_receivers().items()}
                polls.append(
                    {"read_ns": time.monotonic_ns() - start, "heights": heights}
                )
                return min(heights.values()) >= floor

            wait("receivers within four blocks of the source", within_window)
            rows[-1]["receiver_polls"] = polls
    wait(
        "all native stores committed the final loaded tip",
        lambda: all(
            selected == final for _, selected, _ in sample_receivers().values()
        ),
    )
    duration = time.monotonic_ns() - start
    obs = {r["height"]: r for r in status()["mining"]["validation_window"]}
    for row in rows:
        actual = obs[row["height"]]
        assert actual["hash"] == row["hash"]
        row["witnessed_signature_checks"] = actual["script_checks_passed"]
        assert (
            row["witnessed_signature_checks"]
            == actual["transactions_determinate"]
            == fanout
        )
        readbacks = {}
        for label, n in zip(("tn1", "tn2"), NODES):
            selected = rpc(n, "getblockhash", row["height"])
            readbacks[label] = hashlib.sha256(block(n, selected)[0]).hexdigest()
            assert selected == row["hash"]
        selected = sv("getblockhash", row["height"])
        readbacks["sv"] = hashlib.sha256(
            bytes.fromhex(sv("getblock", selected, 0))
        ).hexdigest()
        assert selected == row["hash"] and set(readbacks.values()) == {
            row["wire_sha256"]
        }
        row["independent_wire_sha256"] = readbacks
    caps = subprocess.check_output(
        ["docker", "info", "--format", "{{.Architecture}} {{.NCPU}} {{.MemTotal}}"],
        text=True,
    ).split()
    return {
        "network": "regtest",
        "reference_ingress": "SV submitblock",
        "native_reference_link": "SV wire to Teranode legacy, Teranode native gossip",
        "funding_block": first.hash,
        "funding_header": first.serialize()[:80].hex(),
        "receiver_window": 4,
        "final_native_observations": final_observations,
        "transactions_per_block": fanout,
        "rounds": rounds,
        "measured_wall_ns": duration,
        "transactions_per_second": fanout * rounds * 1e9 / duration,
        "rows": rows,
        "scary_binary_sha256": compose(
            "exec",
            "-T",
            "scary-node",
            "sha256sum",
            "/usr/local/bin/scary-node",
            capture=True,
        ).split()[0],
        "docker": {
            "architecture": caps[0],
            "vcpus": int(caps[1]),
            "memory_bytes": int(caps[2]),
        },
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--rounds", type=int, choices=(8, 32, 64), default=64)
    p.add_argument("--fanout", type=int, choices=(1024, 4096, 16384), default=4096)
    args = p.parse_args()
    with session_lock(), tempfile.TemporaryDirectory() as d:
        result = run(Path(d), args.rounds, args.fanout)
        temporary = args.receipt.with_suffix(args.receipt.suffix + ".next")
        temporary.write_text(json.dumps(result, indent=2) + "\n")
        temporary.replace(args.receipt)
