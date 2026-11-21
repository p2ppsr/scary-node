"""Prepare the common parent and run the registered native replay on independent Aerospike stores."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

# Use only the dedicated loopback ports and named Docker project.
from sustained_profiles import PROJECTS, WITNESSED_ARMS, profile

PROFILE = profile()
PRODUCTION_REPLICATION = PROFILE == "production" or PROFILE in WITNESSED_ARMS
LIVE_CADENCE = PROFILE == "live-cadence"
project, shift = PROJECTS[PROFILE]
os.environ["COMPOSE_PROJECT_NAME"] = project
os.environ["SCARY_LAB_PORT_SHIFT"] = str(shift)
if PROFILE in WITNESSED_ARMS:
    os.environ["SCARY_WITNESSED_PREFIX"] = str((19400 + shift) // 100)
from mixed_corpus import inspect, path_at
from mixed_native_load import NODES, measure, rpc, status, sv, wait
from run_cohort import ROOT, session_lock
from verify_mixed_native_load import verify


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def compose(*args):
    command = ["compose", "-p", os.environ["COMPOSE_PROJECT_NAME"]]
    layers = [
        "compose.yaml",
        "mixed.compose.yaml",
        "mixed-native.compose.yaml",
        "mixed-aerospike.compose.yaml",
    ]
    if PRODUCTION_REPLICATION or LIVE_CADENCE:
        layers += ["sustained-wallet.compose.yaml", "sustained-production.compose.yaml"]
    if PROFILE in WITNESSED_ARMS:
        layers.append("sustained-witnessed.compose.yaml")
    if LIVE_CADENCE:
        layers.append("live-cadence.compose.yaml")
    for name in layers:
        command += ["-f", str(ROOT / "regtest" / name)]
    return docker(*command, *args)


def start_references():
    compose("up", "-d", "--no-build", "--pull", "never", "as1", "as2")

    def stores_ready():
        return all(
            "service ready" in compose("logs", "--no-color", name).lower()
            for name in ("as1", "as2")
        )

    wait("both independent Aerospike namespaces", stores_ready, seconds=240)
    compose(
        "up", "-d", "--no-build", "--pull", "never", "svref", "tn1", "tn2", "ingress"
    )

    def source_connected():
        try:
            return sv("getblockchaininfo")["chain"] == "regtest" and any(
                "teranode" in p["subver"].lower() for p in sv("getpeerinfo")
            )
        except subprocess.CalledProcessError:
            return False

    wait("SV source and native Teranode link", source_connected, seconds=180)


def seed(directory, receipt, last, entrant=False, on_redial=None):
    first = int(sv("getblockcount"))
    if first > last:
        raise ValueError(
            "selected source has advanced past the intended preparation height"
        )
    if first:
        if sv("getbestblockhash") != block_hash(directory, first):
            raise ValueError("incompatible existing source history")
    for height in range(first + 1, last + 1):
        raw = path_at(directory, height).read_bytes()
        h = hashlib.sha256(hashlib.sha256(raw[:80]).digest()).digest()[::-1].hex()
        result = sv("submitblock", raw.hex())
        if result is not None:
            raise ValueError(f"setup block rejected at {height}: {result}")
        if height % 12 == 0 or height == last:
            if (
                first == 0
                and height == 12
                and all(rpc(n, "getblockchaininfo")["blocks"] == 0 for n in NODES)
            ):
                # The genesis-to-genesis handshake advertised zero as the source's
                # height. Redial once so the stock initial-sync peer sees the real tip.
                peer = next(
                    p for p in sv("getpeerinfo") if "teranode" in p["subver"].lower()
                )
                sv("disconnectnode", peer["addr"])
                if on_redial:
                    on_redial(height)
            wait(
                f"stock nodes selected setup block {height}",
                lambda: all(
                    rpc(n, "getblockchaininfo")["bestblockhash"] == h for n in NODES
                ),
                seconds=600,
            )
            if entrant:
                wait(
                    f"entrant selected setup block {height}",
                    lambda: status()["mining"]["tip_hash"] == h,
                    seconds=120,
                )


def block_hash(directory, height):
    with path_at(directory, height).open("rb") as f:
        header = f.read(80)
    return hashlib.sha256(hashlib.sha256(header).digest()).digest()[::-1].hex()


def topology():
    result = {}
    for name in ("as1", "as2", "scary-node", "svref", "tn1", "tn2", "ingress"):
        info = json.loads(docker("inspect", compose("ps", "-q", name)))[0]
        image = json.loads(docker("image", "inspect", info["Image"]))[0]
        host = info["HostConfig"]
        result[name] = {
            "image_ref": info["Config"]["Image"],
            "image_id": info["Image"],
            "repository_digests": image.get("RepoDigests") or [],
            "architecture": image["Architecture"],
            "memory_limit_bytes": host["Memory"],
            "nano_cpus_limit": host["NanoCpus"],
            "cpu_quota": host["CpuQuota"],
            "cpu_period": host["CpuPeriod"],
            "cpuset": host["CpusetCpus"],
            "mounts": [
                {
                    "target": m["Destination"],
                    "type": m["Type"],
                    "volume": m.get("Name"),
                    "read_write": m["RW"],
                }
                for m in info["Mounts"]
            ],
            "networks": sorted(info["NetworkSettings"]["Networks"]),
        }
        if name in ("as1", "as2"):
            df = (
                docker("exec", info["Id"], "df", "-Pk", "/opt/aerospike/data")
                .splitlines()[-1]
                .split()
            )
            result[name]["backing_size_bytes"] = int(df[1]) * 1024
            result[name]["backing_available_bytes"] = int(df[3]) * 1024
            result[name]["registered_datafile_bytes"] = 9 * 1024**3
        if name in ("tn1", "tn2"):
            result[name]["utxo_store"] = next(
                x.split("=", 1)[1]
                for x in info["Config"]["Env"]
                if x.startswith("utxostore=")
            )
    return result


def run(args):
    receipt = json.loads((args.corpus / "sqlite-receipt.json").read_text())
    with session_lock():
        details = inspect(args.corpus, receipt)
        if details != json.loads((args.corpus / "manifest.json").read_text()):
            raise ValueError("corpus manifest differs")
        if (receipt["rounds"], receipt["transactions_per_block"]) != (32, 16384):
            raise ValueError("registered 32 x 16,384 workload required")
        height = details["funding_height"]
        start_references()
        if args.action == "prepare":
            # Leave one contemporary pre-funding block to establish the entrant's boundary.
            seed(args.corpus, receipt, height - 2)
            print(f"Full nodes selected archived height {height - 2}.")
            return
        journal = args.receipt.with_suffix(".attempt.jsonl")
        with journal.open("x", buffering=1) as log:

            def record(event):
                log.write(json.dumps(event) + "\n")
                log.flush()

            try:
                record(
                    {"event": "preflight", "wall_ns": time.time_ns(), "corpus": details}
                )
                candidate_image = docker(
                    "image", "inspect", "--format", "{{.Id}}", args.image
                )
                if candidate_image != args.image_id:
                    raise ValueError(
                        "local release image differs from expected immutable image ID"
                    )
                os.environ["SCARY_NODE_IMAGE"] = args.image
                seed(args.corpus, receipt, height - 2)
                compose(
                    "up", "-d", "--no-build", "--pull", "never", "scary-node", "ingress"
                )

                def connected():
                    return status()["mining"]["connected_data_peers"] >= 3

                wait("entrant connected to both native stacks", connected, seconds=180)
                binary = compose(
                    "exec", "-T", "scary-node", "sha256sum", "/usr/local/bin/scary-node"
                ).split()[0]
                if binary != args.binary_sha256:
                    raise ValueError("committed Linux executable digest differs")
                seed(args.corpus, receipt, height - 1, entrant=True)
                seed(args.corpus, receipt, height, entrant=True)

                def funding_witnessed():
                    return any(
                        row["hash"] == receipt["funding_block"]
                        for row in status()["mining"]["validation_window"]
                    )

                wait("witnessed exact funding block", funding_witnessed)
                mining = status()["mining"]
                interpreter = mining["interpreter"]
                if (
                    (
                        interpreter["linked_bdk_version"],
                        interpreter["linked_sv_version"],
                    )
                    != ("1.4.0", "1.2.3")
                    or not interpreter["bdk_commit"].startswith("8a4e1d3d")
                    or not interpreter["sv_node_commit"].startswith("6504a3af")
                ):
                    raise ValueError(
                        "entrant reported an unexpected linked interpreter build"
                    )
                if mining["tip_hash"] != receipt["funding_block"]:
                    raise ValueError("entrant reported an unexpected funding parent")
                before = topology()
                entry_state = {
                    k: mining[k]
                    for k in (
                        "height",
                        "tip_hash",
                        "interpreter",
                        "connected_data_peers",
                        "observation_window_ready",
                        "storage_failure",
                    )
                }
                record(
                    {
                        "event": "entrant_verified",
                        "binary_sha256": binary,
                        "image_id": candidate_image,
                        "height": mining["height"],
                        "parent": mining["tip_hash"],
                    }
                )
                if before["scary-node"]["image_id"] != candidate_image:
                    raise ValueError("running entrant image differs")
                blocks = [
                    (r["hash"], path_at(args.corpus, r["height"]))
                    for r in receipt["rows"]
                ]
                result = measure(
                    blocks,
                    height,
                    16384,
                    receipt["funding_block"],
                    receipt["funding_header"],
                    progress=record,
                )
                for ref, measured in zip(receipt["rows"], result["rows"], strict=True):
                    if (ref["hash"], ref["wire_sha256"]) != (
                        measured["hash"],
                        measured["wire_sha256"],
                    ):
                        raise ValueError("archived benchmark workload differs")
                summary = verify(result)
                first_offer = result["rows"][0]["offered_ns"]
                seconds = (result["rows"][-1]["source_accepted_ns"] - first_offer) / 1e9
                adoption_seconds = (result["measured_wall_ns"] - first_offer) / 1e9
                source_rate = summary["signed_spends"] / seconds
                result["storage_trial"] = {
                    "registration": "9963c88d116095aa25adf934addf65173bba1e15",
                    "source_revision": "af9b605ad8da96d041a540111d46a7c0105209f7",
                    "corpus": details,
                    "topology": before,
                    "entry_state": entry_state,
                    "first_offer_to_final_adoption_s": adoption_seconds,
                    "first_offer_to_final_adoption_spends_per_s": summary[
                        "signed_spends"
                    ]
                    / adoption_seconds,
                    "source_until_last_acceptance_s": seconds,
                    "source_elapsed_spends_per_s": source_rate,
                    "target_spends_per_s": 3000,
                    "both_targets_met": min(
                        source_rate, summary["signed_spends"] / adoption_seconds
                    )
                    >= 3000,
                }
                tmp = args.receipt.with_suffix(".next")
                tmp.write_text(json.dumps(result, indent=2) + "\n")
                tmp.replace(args.receipt)
                record(
                    {"event": "verified", "wall_ns": time.time_ns(), "summary": summary}
                )
            except Exception as exc:
                record(
                    {
                        "event": "incomplete",
                        "wall_ns": time.time_ns(),
                        "reason": str(exc),
                    }
                )
                raise


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["prepare", "measure"])
    p.add_argument("corpus", type=Path)
    p.add_argument("--receipt", type=Path)
    p.add_argument("--image")
    p.add_argument("--image-id")
    p.add_argument("--binary-sha256")
    args = p.parse_args()
    if args.action == "measure" and not all(
        (args.receipt, args.image, args.image_id, args.binary_sha256)
    ):
        p.error("measure requires --receipt, --image, --image-id and --binary-sha256")
    run(args)
