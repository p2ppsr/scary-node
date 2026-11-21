"""Reconstruct a measured regtest branch on stock Teranode/Aerospike using a fresh stock SV peer."""

import argparse
import contextlib
import hashlib
import json
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

from cadence_transport import offer
from mixed_native import rpc, wait
from recover_sustained_chain import (
    FUNDING,
    GIB,
    HISTORY,
    REGTEST_GENESIS,
    digest,
    observed_wire_stream,
    path_at,
    read,
    selected_order,
)
from restore_witness_archive import HELPER, canonical, observation
from run_cohort import ROOT, session_lock
from sustained_profiles import ENDORSEMENT
from sv_reference import IMAGE as SV_IMAGE
from sv_reference import Reference
from verify_receipt import require
from verify_sustained_native import dsha, observed_bodies

TERANODE_IMAGE = "ghcr.io/bsv-blockchain/teranode:v0.16.1@sha256:84971b2863301c7527382768dbe5a094cd8c4d6e2f426f5557d3c12ba114822a"
AEROSPIKE_IMAGE = "ghcr.io/bsv-blockchain/aerospike-server:8.1.2.0-1@sha256:83ac381fdad8b1ec73392c3811958b828ab0801fb032a9ec58e63e07e9fb1cbc"


def sha(wire):
    return hashlib.sha256(wire).hexdigest()


def identity(wire):
    return dsha(wire[:80])[::-1].hex()


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


@contextlib.contextmanager
def references(cadence):
    project = "scary-restore-" + uuid.uuid4().hex[:12]
    args = ["compose", "-p", project]
    files = [
        "compose.yaml",
        "mixed.compose.yaml",
        "mixed-native.compose.yaml",
        "mixed-aerospike.compose.yaml",
        "sustained-wallet.compose.yaml",
        "sustained-production.compose.yaml",
    ]
    if cadence:
        files.append("live-cadence.compose.yaml")
    for name in [*files, "chain-recovery.compose.yaml"]:
        args.extend(["-f", str(ROOT / "regtest" / name)])

    def compose(*values):
        return docker(*args, *values)

    def start(*names):
        compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", *names)

    def port(service, exposed):
        return int(compose("port", service, exposed).rsplit(":", 1)[1])

    require(not compose("ps", "-aq"), "temporary namespace already has a container")
    mem = docker(
        "run",
        "--rm",
        "--read-only",
        "--pull=never",
        "--network",
        "none",
        "--log-driver",
        "none",
        "--entrypoint",
        "cat",
        HELPER,
        "/proc/meminfo",
    )
    avail = (
        int(
            next(
                line for line in mem.splitlines() if line.startswith("MemAvailable:")
            ).split()[1]
        )
        * 1024
    )
    require(
        avail >= 38 * GIB and shutil.disk_usage(ROOT).free >= 20 * GIB,
        "insufficient isolated replay RAM or host reserve",
    )
    try:
        start("as2")
        wait(
            "isolated Aerospike namespace",
            lambda: "service ready" in compose("logs", "as2").lower(),
            seconds=240,
        )
        start("svref", "tn2", "ingress", "wallet-ingress")
        stock = Reference()
        stock.container = compose("ps", "-q", "svref")
        stock.ready()
        endpoint = (port("ingress", "19442"), port("ingress", "19452"))
        peer_port = port("wallet-ingress", "21444")
        wait(
            "stock SV to Teranode handshake",
            lambda: any(
                "teranode" in p["subver"].lower() for p in stock.rpc("getpeerinfo")
            ),
            seconds=240,
        )
        wait(
            "fresh stock Teranode",
            lambda: (
                rpc(endpoint, "getblockchaininfo")["bestblockhash"] == REGTEST_GENESIS
            ),
            seconds=240,
        )
        require(
            stock.rpc("getbestblockhash") == REGTEST_GENESIS,
            "stock SV did not start at genesis",
        )
        services = {}
        for service, wanted in (
            ("svref", {"/data"}),
            ("tn2", {"/app/data"}),
            ("as2", {"/opt/aerospike/data", "/opt/aerospike/smd"}),
        ):
            config = json.loads(docker("inspect", compose("ps", "-q", service)))[0]
            mounts = {m["Destination"]: m["Type"] for m in config["Mounts"]}
            require(
                all(mounts.get(d) == "tmpfs" for d in wanted),
                "reference store was not initially empty",
            )
            services[service] = {
                "image": config["Config"]["Image"],
                "image_id": config["Image"],
                "temporary_data": sorted(wanted),
            }
        yield stock, endpoint, peer_port, services, avail
    finally:
        compose("down", "--timeout", "10", "--volumes")


def read_asset(endpoint, h, size, digest):
    url = f"http://127.0.0.1:{endpoint[1]}/api/v1/block_legacy/{h}?wire=1"
    s = hashlib.sha256()
    n = 0
    with urllib.request.urlopen(url, timeout=600) as stream:
        while chunk := stream.read(1 << 20):
            n += len(chunk)
            s.update(chunk)
    require(
        n == size and s.hexdigest() == digest,
        "fresh Teranode full bytes differ from original peers",
    )
    return {"hash": h, "wire_sha256": s.hexdigest(), "wire_bytes": n}


def run(args):
    cadence = args.action == "cadence"
    measured = observation(args.measured)
    elapsed = time.monotonic()
    hits = []
    redials = []
    detached = {}
    events = []
    with references(cadence) as (stock, endpoint, port, services, avail):

        def wait_selected(h):
            wait(
                "fresh native Teranode adoption",
                lambda: rpc(endpoint, "getblockchaininfo")["bestblockhash"] == h,
                seconds=900,
            )

        def submit(wire):
            require(
                stock.rpc("submitblock", wire.hex()) is None,
                "fresh stock SV rejected the archived block",
            )
            return identity(wire)

        branch = hashlib.sha256()
        for height in range(1, 484):
            wire = path_at(args.history, height).read_bytes()
            branch.update(hashlib.sha256(wire).digest())
            h = submit(wire)
            if height == 12 and rpc(endpoint, "getblockchaininfo")["blocks"] == 0:
                peer = next(
                    p
                    for p in stock.rpc("getpeerinfo")
                    if "teranode" in p["subver"].lower()
                )
                stock.rpc("disconnectnode", peer["addr"])
                redials.append(height)
            if height % 12 == 0 or height == 483:
                wait_selected(h)
        require(branch.hexdigest() == HISTORY, "wrong common wire history")
        h = submit((args.corpus / "funding.block").read_bytes())
        require(h == FUNDING, "wrong split funding")
        wait_selected(h)
        if cadence:
            body = args.body
            fixture = read(ROOT / "docs/evidence/chronicle-live-cadence-body.json")
            require(
                digest(body) == fixture["wire_sha256"]
                and body.stat().st_size == fixture["wire_bytes"],
                "wrong cadence fixture",
            )
            require(
                measured["fixture_hash"] == fixture["block_hash"],
                "original observed fixture differs",
            )
            offer(body, fixture["wire_sha256"], port, 485, events.append)
            wait_selected(fixture["block_hash"])
            hits.append(
                {
                    "height": 485,
                    **read_asset(
                        endpoint,
                        fixture["block_hash"],
                        fixture["wire_bytes"],
                        fixture["wire_sha256"],
                    ),
                }
            )
            for height, encoded in enumerate(measured["successor_hex"], 486):
                raw = bytes.fromhex(encoded)
                h = submit(raw)
                wait_selected(h)
                hits.append(
                    {"height": height, **read_asset(endpoint, h, len(raw), sha(raw))}
                )
            expected = measured["restored"]
            height, terminal = expected["height"], expected["tip_hash"]
        else:
            if measured.get("arm") == "endorsed":
                h = submit(ENDORSEMENT)
                wait_selected(h)
            require(h == measured["baseline"]["hash"], "wrong pre-offer boundary")
            rows = observed_wire_stream(
                args.measured, args.corpus, args.blocks, args.archive
            )
            for row, raw in selected_order(measured["blocks"], rows, detached):
                h = submit(raw)
                wait_selected(h)
                hits.append(
                    {
                        "height": row["height"],
                        **read_asset(
                            endpoint, h, row["wire_bytes"], row["wire_sha256"]
                        ),
                    }
                )
            height, terminal = (
                measured["completion"]["final_height"],
                measured["completion"]["final_hash"],
            )
        tn = rpc(endpoint, "getblockchaininfo")
        sv = stock.rpc("getblockchaininfo")
        require(
            all(
                o["blocks"] == height and o["bestblockhash"] == terminal
                for o in (sv, tn)
            ),
            "terminal chain differs",
        )
        return {
            "action": args.action,
            "network": "regtest",
            "transport": "fresh SV Node to fresh Teranode legacy peer; native Asset full readback",
            "services": services,
            "starting_hash": REGTEST_GENESIS,
            "starting_height": 0,
            "common_history_wire_commitment": HISTORY,
            "common_history_blocks": 483,
            "funding_hash": FUNDING,
            "preflight_guest_available_bytes": avail,
            "genesis_resume_redials": redials,
            "large_native_sv_events": events,
            "measured_observation_sha256": digest(args.measured),
            "measured_record_sha256": canonical(measured),
            "selected_readbacks": hits,
            "separately_checked_detached": detached,
            "height": height,
            "tip_hash": terminal,
            "sv_chainwork": sv["chainwork"],
            "elapsed_s_including_setup": time.monotonic() - elapsed,
        }


def verify(proof, original):
    measured = observation(original)
    require(
        proof["network"] == "regtest"
        and proof["starting_hash"] == REGTEST_GENESIS
        and proof["starting_height"] == 0
        and proof["common_history_wire_commitment"] == HISTORY
        and proof["common_history_blocks"] == 483
        and proof["funding_hash"] == FUNDING
        and (
            proof.get("measured_record_sha256") == canonical(measured)
            or proof["measured_observation_sha256"]
            in (digest(original), sha((json.dumps(measured, indent=2) + "\n").encode()))
        ),
        "independent recovery has different inputs",
    )
    services = proof["services"]
    for name, image, dirs in (
        ("svref", SV_IMAGE, ["/data"]),
        ("tn2", TERANODE_IMAGE, ["/app/data"]),
        ("as2", AEROSPIKE_IMAGE, ["/opt/aerospike/data", "/opt/aerospike/smd"]),
    ):
        item = services[name]
        require(
            item["image"] == image
            and len(bytes.fromhex(item["image_id"].removeprefix("sha256:"))) == 32
            and item["temporary_data"] == dirs,
            "different stock image or replay filesystem",
        )
    require(
        proof["genesis_resume_redials"] in ([], [12])
        and int(proof["sv_chainwork"], 16) > 0,
        "invalid work or initial redial observation",
    )
    if proof["action"] == "cadence":
        require(
            proof["large_native_sv_events"]
            == ["ready_to_announce", "stock_requested", "body_written", "stock_pong"]
            and not proof["separately_checked_detached"],
            "different large block handoff",
        )
        fixture = read(ROOT / "docs/evidence/chronicle-live-cadence-body.json")
        expected = [
            {
                "height": 485,
                "hash": fixture["block_hash"],
                "wire_sha256": fixture["wire_sha256"],
                "wire_bytes": fixture["wire_bytes"],
            }
        ]
        for h, w in enumerate(measured["successor_hex"], 486):
            data = bytes.fromhex(w)
            expected.append(
                {
                    "height": h,
                    "hash": identity(data),
                    "wire_sha256": sha(data),
                    "wire_bytes": len(data),
                }
            )
    else:
        selected = {row["hash"] for row in measured["blocks"]}
        require(
            not proof["large_native_sv_events"]
            and proof["separately_checked_detached"]
            == {
                h: b["wire_sha256"]
                for h, b in observed_bodies(measured).items()
                if h not in selected
            },
            "detached set differs",
        )
        expected = [
            {k: b[k] for k in ("height", "hash", "wire_sha256", "wire_bytes")}
            for b in measured["blocks"]
        ]
    require(
        expected
        and expected == proof["selected_readbacks"]
        and (proof["height"], proof["tip_hash"])
        == (expected[-1]["height"], expected[-1]["hash"]),
        "Teranode receipts differ",
    )
    return {
        "height": proof["height"],
        "tip_hash": proof["tip_hash"],
        "full_native_readbacks": len(expected),
        "readback_bytes": sum(r["wire_bytes"] for r in expected),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("cadence", "sustained", "verify"))
    p.add_argument("measured", type=Path)
    p.add_argument("proof", type=Path)
    p.add_argument("--history", type=Path)
    p.add_argument("--corpus", type=Path)
    p.add_argument("--body", type=Path)
    case = p.add_mutually_exclusive_group()
    case.add_argument("--archive", type=Path)
    case.add_argument("--blocks", type=Path)
    a = p.parse_args()
    if a.action != "verify":
        if a.proof.exists() or not all(
            (
                a.history,
                a.corpus,
                a.body if a.action == "cadence" else (a.blocks or a.archive),
            )
        ):
            p.error("provide the replay inputs and choose an unused proof path")
        with session_lock():
            out = run(a)
            verify(out, a.measured)
            with a.proof.open("x") as f:
                json.dump(out, f, indent=2)
                f.write("\n")
    print(json.dumps(verify(read(a.proof), a.measured), indent=2))
