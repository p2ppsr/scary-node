"""Check bidirectional native successor propagation on the existing isolated mixed regtest."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "regtest"))
os.environ.setdefault("COMPOSE_PROJECT_NAME", "scary-node-mixed")
os.environ["SCARY_LAB_MIXED"] = "1"
from prove import NODES, assert_tip, block, mixed_sv, rpc, status, txid, wait


def run():
    a, b = NODES
    parent = mixed_sv("getbestblockhash")
    assert (
        assert_tip(a, parent)
        and status()["mining"]["tip_hash"] == parent
    )
    command = ["docker", "compose", "-p", os.environ["COMPOSE_PROJECT_NAME"]]
    for layer in ["compose.yaml", "mixed.compose.yaml", "mixed-native.compose.yaml"]:
        command += ["-f", str(ROOT / "regtest" / layer)]
    subprocess.run(command + ["up", "-d", "--no-deps", "tn2"], check=True)
    wait("restarted Teranode reopened common parent", lambda: assert_tip(b, parent))

    def link():
        return next(
            (
                peer["subver"]
                for peer in json.loads(mixed_sv("getpeerinfo"))
                if "teranode" in peer["subver"].lower()
            ),
            None,
        )

    agent = wait("stock native SV/Teranode version handshake", link)
    first = parent
    rows = []
    for i in range(12):
        issuer = "sv" if i % 2 == 0 else "tn2"
        start = time.monotonic_ns()
        if issuer == "sv":
            h = json.loads(mixed_sv("generate", 1))[0]
        else:
            h = rpc(b, "generate", 1)[0]
        wait(
            "two Teranode chains select native successor",
            lambda: all(assert_tip(n, h) for n in NODES),
        )
        wait(
            "SV chain selects native successor",
            lambda: mixed_sv("getbestblockhash") == h,
        )
        wait(
            "entrant commits native successor",
            lambda: status()["mining"]["tip_hash"] == h,
        )
        heads = {
            "sv": mixed_sv("getbestblockhash"),
            "tn1": rpc(a, "getblockchaininfo")["bestblockhash"],
            "tn2": rpc(b, "getblockchaininfo")["bestblockhash"],
            "entrant": status()["mining"]["tip_hash"],
        }
        assert set(heads.values()) == {h}
        elapsed = time.monotonic_ns() - start
        raw, _, _ = block(a, h)
        assert raw == block(b, h)[0] == bytes.fromhex(mixed_sv("getblock", h, 0))
        assert h == txid(raw[:80]) and raw[4:36][::-1].hex() == parent
        rows.append(
            {
                "issuer": issuer,
                "hash": h,
                "wire_hex": raw.hex(),
                "wire_sha256": hashlib.sha256(raw).hexdigest(),
                "all_active_after_ns": elapsed,
                "receiver_hashes": heads,
            }
        )
        parent = h
    return {
        "network": "regtest",
        "teranode_release": "v0.16.1",
        "sv_node_release": "1.2.3",
        "reference_peer_agent": agent,
        "starting_parent": first,
        "rows": rows,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    args = p.parse_args()
    args.receipt.write_text(json.dumps(run(), indent=2) + "\n")
