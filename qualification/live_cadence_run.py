"""Measure native receipt of a single contemporary-sized signed block on four isolated regtest nodes."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from http.client import HTTPException
from pathlib import Path
from urllib.request import urlopen

os.environ["SCARY_SUSTAINED_PROFILE"] = "live-cadence"
# Select the isolated project and ports before importing the shared proof client.
from mixed_storage_trial import (
    NODES,
    ROOT,
    compose,
    docker,
    rpc,
    seed,
    session_lock,
    start_references,
    status,
    sv,
    topology,
)

# isort: split
from cadence_transport import offer, read_stock
from live_cadence_body import COUNT, FUNDING, HEIGHT
from sustained_native import prepare_funding
from sustained_resources import GIB, sample_resources

PROJECT = "scary-node-live-cadence"
PORT = 23444
ELF = "063f28035d8a6e3f7f69d858aafdd448b6c4bba9ebee2f350423bdf53bb4bafe"
MIN_FREE = 20 * GIB


def save(path, data):
    tmp = path.with_suffix(path.suffix + ".next")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def plain_tips():
    return {
        "sv": sv("getbestblockhash"),
        "tn1": rpc(NODES[0], "getblockchaininfo")["bestblockhash"],
        "tn2": rpc(NODES[1], "getblockchaininfo")["bestblockhash"],
        "scary": status()["mining"]["tip_hash"],
    }


def prepare(history, payments, directory, image, prepared_body=None):
    if (
        NODES != [(23441, 23451), (23442, 23452)]
        or os.environ["COMPOSE_PROJECT_NAME"] != PROJECT
    ):
        raise ValueError("incorrect regtest project")
    log = directory / "setup.json"
    initial = not log.exists()
    if initial:
        resources = docker(
            "volume",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
        )
        if resources or docker(
            "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"
        ):
            raise ValueError("a new enrollment requires a fresh isolated project")
        # Part of staging can already occupy the host filesystem before setup.
        # Credit only the allocated bytes of the independently verified fixture.
        allocated_body = 0
        if prepared_body is not None:
            fixture = json.loads(
                (ROOT / "docs/evidence/chronicle-live-cadence-body.json").read_bytes()
            )
            details = prepared_body.stat()
            with prepared_body.open("rb") as stream:
                valid = (
                    details.st_size == fixture["wire_bytes"]
                    and hashlib.file_digest(stream, "sha256").hexdigest()
                    == fixture["wire_sha256"]
                )
            if not valid:
                raise ValueError(
                    "prepared cadence body differs from the registered fixture"
                )
            if details.st_dev == directory.stat().st_dev:
                allocated_body = min(details.st_size, details.st_blocks * 512)
        if shutil.disk_usage(directory).free < MIN_FREE + 14 * GIB - allocated_body:
            raise ValueError(
                "preparation needs space for two 5-GiB stores, staging and the 20-GiB reserve"
            )
        save(
            log,
            {
                "project": PROJECT,
                "starting_host_free_bytes": shutil.disk_usage(directory).free,
                "fixture_bytes_already_allocated": allocated_body,
                "events": [],
            },
        )
    setup = json.loads(log.read_text())
    if setup["project"] != PROJECT or (directory / "measurement.jsonl").exists():
        raise ValueError("setup belongs to another project or an already measured run")

    def event(name, **values):
        setup["events"].append(dict(event=name, wall_ns=time.time_ns(), **values))
        save(log, setup)

    os.environ["SCARY_NODE_IMAGE"] = image
    image_id = docker("image", "inspect", "--format", "{{.Id}}", image)
    start_references()
    free = (
        int(
            compose("exec", "-T", "svref", "df", "-Pk", "/data")
            .splitlines()[-1]
            .split()[3]
        )
        * 1024
    )
    if free < 16 * GIB or shutil.disk_usage(directory).free < MIN_FREE:
        raise ValueError("disk reserve unavailable after namespace initialization")
    receipt = json.loads((history / "sqlite-receipt.json").read_text())
    if receipt != json.loads(
        (ROOT / "docs/evidence/chronicle-mixed-native-large.json").read_text()
    ):
        raise ValueError("funding history receipt differs")
    current = int(sv("getblockcount"))
    if current > HEIGHT - 1:
        raise ValueError("setup chain has advanced past enrollment")
    if current < 449:
        seed(history, receipt, 449, on_redial=lambda h: event("setup_redial", height=h))
    compose(
        "up",
        "-d",
        "--no-build",
        "--pull",
        "never",
        "scary-node",
        "ingress",
        "wallet-ingress",
    )
    from prove import wait

    wait(
        "two native stacks and SV linked",
        lambda: status()["mining"]["connected_data_peers"] >= 3,
        seconds=180,
    )
    if status()["release"]["executable_sha256"] != ELF:
        raise ValueError("unexpected Linux release executable")
    for height in (450, 451, 483):
        if int(sv("getblockcount")) < height:
            seed(history, receipt, height, entrant=True)
    if not (payments / "funding.block").is_file():
        raise ValueError("missing published height-484 funding bytes")
    funding = prepare_funding(payments)
    if funding["hash"] != FUNDING or set(plain_tips().values()) != {FUNDING}:
        raise ValueError("four nodes did not select the funding block")
    observed = status()["mining"]
    if not any(
        r["hash"] == receipt["funding_block"] for r in observed["validation_window"]
    ):
        raise ValueError("entrant missed the archived funding history")
    setup.update(
        topology=topology(),
        funding=funding,
        capacity=observed["capacity"],
        executable_sha256=ELF,
        image_id=image_id,
        docker_free_bytes=free,
    )
    if any(
        not m["volume"].startswith(PROJECT + "_")
        for service in setup["topology"].values()
        for m in service["mounts"]
        if m["type"] == "volume"
    ):
        raise ValueError("state volume belongs to another project")
    event("ready", selected=FUNDING)


def peer_digest(node, block_hash):
    url = f"http://127.0.0.1:{node[1]}/api/v1/block_legacy/{block_hash}?wire=1"
    with urlopen(url, timeout=600) as stream:
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return {"wire_bytes": size, "wire_sha256": digest.hexdigest()}


def measure(directory, body, fixture, image):
    os.environ["SCARY_NODE_IMAGE"] = image
    setup = json.loads((directory / "setup.json").read_text())
    if (
        setup["events"][-1]["event"] != "ready"
        or status()["release"]["executable_sha256"] != ELF
    ):
        raise ValueError("prepare the registered release first")
    ident, size, digest = (
        fixture["block_hash"],
        fixture["wire_bytes"],
        fixture["wire_sha256"],
    )
    if (fixture["height"], fixture["funding_parent"], fixture["payment_count"]) != (
        HEIGHT,
        FUNDING,
        COUNT,
    ):
        raise ValueError("fixture differs from the registered positive block")
    with body.open("rb") as f:
        if (
            hashlib.file_digest(f, "sha256").hexdigest() != digest
            or body.stat().st_size != size
        ):
            raise ValueError("fixture body and manifest differ")
    if set(plain_tips().values()) != {FUNDING}:
        raise ValueError(
            "all four nodes must select the funding parent before enrollment"
        )
    # The first native announcement starts the rate denominator. Chronology and
    # readbacks survive a failed or interrupted attempt in the append-only log.
    base = time.monotonic_ns()
    stop, resource_stop = threading.Event(), threading.Event()
    result = {
        "project": PROJECT,
        "network": "regtest",
        "executable_sha256": ELF,
        "fixture": fixture,
        "starting_parent": FUNDING,
        "observations": {},
        "reads": {},
        "completed": False,
    }
    lock = threading.Lock()
    with (directory / "measurement.jsonl").open("x", buffering=1) as log:

        def stamp(name, **detail):
            elapsed = time.monotonic_ns() - base
            with lock:
                log.write(
                    json.dumps(dict(event=name, elapsed_ns=elapsed, **detail)) + "\n"
                )
            return elapsed

        def until_tip(name, read, expected):
            deadline = time.monotonic() + 1800
            while (
                time.monotonic() < deadline
                and not stop.is_set()
                and not resource_stop.is_set()
            ):
                try:
                    value = read()
                except (
                    OSError,
                    HTTPException,
                    subprocess.SubprocessError,
                    RuntimeError,
                    ValueError,
                ) as exc:
                    stamp("peer_poll_error", peer=name, error=type(exc).__name__)
                else:
                    if value == expected:
                        return stamp("selected", peer=name, block_hash=expected)
                stop.wait(1)
            raise RuntimeError("selection deadline or resource stop: " + name)

        readers = {
            "scary": lambda: status()["mining"]["tip_hash"],
            "sv": lambda: sv("getbestblockhash"),
            **{
                n: (lambda node=node: rpc(node, "getblockchaininfo")["bestblockhash"])
                for n, node in zip(("tn1", "tn2"), NODES)
            },
        }

        def reference_read(name):
            if name == "sv":
                raw = {
                    "wire_bytes": size,
                    "wire_sha256": read_stock(PORT, HEIGHT, ident, size, digest),
                }
            else:
                raw = peer_digest(NODES[0 if name == "tn1" else 1], ident)
            raw["elapsed_ns"] = stamp("body_read", peer=name, **raw)
            return raw

        sampler = threading.Thread(
            target=sample_resources,
            args=(directory / "resources.jsonl", base, stop, resource_stop),
            daemon=True,
        )
        sampler.start()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        try:
            # The observation timers and native transport share a monotonic clock.
            announcement = {}

            def transport_event(name):
                at = stamp(name)
                announcement[name] = at

            producer = pool.submit(offer, body, digest, PORT, HEIGHT, transport_event)
            pending = {
                pool.submit(until_tip, n, f, ident): n for n, f in readers.items()
            }
            result["errors"] = {}

            def collect(futures, bucket):
                for future in concurrent.futures.as_completed(futures):
                    name = futures[future]
                    try:
                        result[bucket][name] = future.result()
                    except (
                        OSError,
                        HTTPException,
                        RuntimeError,
                        ValueError,
                        subprocess.SubprocessError,
                    ) as error:
                        key = bucket + "/" + name
                        result["errors"][key] = str(error)[:300]
                        stamp(
                            "measurement_error",
                            stage=bucket,
                            peer=name,
                            error=type(error).__name__,
                        )

            collect(pending, "observations")
            try:
                producer.result()
            except (
                OSError,
                RuntimeError,
                ValueError,
                subprocess.SubprocessError,
            ) as error:
                result["errors"]["transport"] = str(error)[:300]
            result["transport"] = announcement
            if resource_stop.is_set():
                raise RuntimeError("workstation resource stop")
            if "scary" in result["observations"]:
                checks = next(
                    r
                    for r in status()["mining"]["validation_window"]
                    if r["hash"] == ident
                )
                result["witnessed_validation"] = checks
                if (
                    any(
                        checks[k] != COUNT
                        for k in (
                            "known_inputs",
                            "script_checks_passed",
                            "amount_checks_passed",
                            "transactions_determinate",
                        )
                    )
                    or checks["same_block_inputs"] != COUNT - 65536
                    or not checks["coinbase_reward_checked"]
                ):
                    result["errors"]["validation"] = (
                        "incomplete witnessed input, fee or script accounting"
                    )
            futures = {
                pool.submit(reference_read, name): name
                for name in ("sv", "tn1", "tn2")
                if name in result["observations"]
            }
            collect(futures, "reads")
            for name, item in result["reads"].items():
                if (item["wire_bytes"], item["wire_sha256"]) != (size, digest):
                    result["errors"]["reads/" + name] = (
                        "stored body digest or length differs"
                    )
            result["settled"] = {}
            if "sv" in result["observations"] and not resource_stop.is_set():
                successors = sv("generate", 2)
                result["successors"] = [sv("getblockheader", s, 0) for s in successors]
                if (
                    sv("getblockheader", successors[0])["previousblockhash"] != ident
                    or sv("getblockheader", successors[1])["previousblockhash"]
                    != successors[0]
                ):
                    raise ValueError("stock successors do not extend the fixture")
                collect(
                    {
                        pool.submit(until_tip, n, f, successors[-1]): n
                        for n, f in readers.items()
                    },
                    "settled",
                )
            if (
                set(result["observations"]) == set(readers)
                and "ready_to_announce" in announcement
            ):
                duration = (
                    max(result["observations"].values())
                    - announcement["ready_to_announce"]
                )
                result.update(
                    four_node_elapsed_ns=duration,
                    payments_per_second=COUNT * 1e9 / duration,
                )
            result["completed"] = (
                not result["errors"]
                and len(result["settled"]) == 4
                and len(result["reads"]) == 3
                and "stock_pong" in announcement
            )
        except Exception as exc:
            result["failure"] = type(exc).__name__ + ": " + str(exc)[:500]
            raise
        finally:
            stop.set()
            if resource_stop.is_set():
                compose("pause")
            pool.shutdown(wait=True, cancel_futures=True)
            sampler.join(10)
            result["resource_stop"] = resource_stop.is_set()
            result["completed"] = result["completed"] and not result["resource_stop"]
            result["cadence_target_met"] = (
                result["completed"] and result["payments_per_second"] >= 3000
            )
            stamp(
                "finished",
                completed=result["completed"],
                cadence_target_met=result["cadence_target_met"],
            )
            save(directory / "result.json", result)
    if not result["completed"]:
        raise ValueError(
            "incomplete native cadence measurement; see the preserved receipt"
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    pre = sub.add_parser("prepare")
    pre.add_argument("history", type=Path)
    pre.add_argument("payments", type=Path)
    pre.add_argument(
        "--prepared-body",
        type=Path,
        help="already allocated verified fixture on this filesystem",
    )
    run = sub.add_parser("run")
    run.add_argument("body", type=Path)
    run.add_argument("manifest", type=Path)
    for cmd in (pre, run):
        cmd.add_argument("directory", type=Path)
        cmd.add_argument(
            "--image",
            required=True,
            help="local Docker image built from the pinned release",
        )
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    with session_lock():
        if args.action == "prepare":
            prepare(
                args.history,
                args.payments,
                args.directory,
                args.image,
                args.prepared_body,
            )
        else:
            measure(
                args.directory,
                args.body,
                json.loads(args.manifest.read_bytes()),
                args.image,
            )
