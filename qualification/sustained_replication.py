"""Initialize separate independent stores, replay the common signed funding and run the inventory cohort."""

import argparse
import json
import os
import shutil
import time
from pathlib import Path

os.environ["SCARY_SUSTAINED_PROFILE"] = "production"
from mixed_storage_trial import (
    NODES,
    compose,
    docker,
    rpc,
    seed,
    start_references,
    status,
    sv,
    topology,
    wait,
)
from mixed_corpus import inspect
from sustained_native import REGISTRATION, RELEASE, prepare_funding, run
from sustained_resources import GIB

IMAGE = "scary-node:throughput-af9b605-5d3fe94e"
IMAGE_ID = "sha256:5d3fe94ee11ed4170cc39a074967c220757ed6078b0df623d7054b35afc55f4a"
FUNDING = "3fb3a456448fd6d91801a35c04112ffab3f30dc0a6a14e426effff4f91660ba9"
SOURCE = "243170edb3df9bea289c32207732d988f6e4b92f67b17634a72e709ae8b735a2"
PROJECT = "scary-node-sustained-production"


def setup(corpus, payments, directory, image=IMAGE, image_id=IMAGE_ID):
    record = {"registration": REGISTRATION, "project": PROJECT, "events": []}
    logfile = directory / "setup.json"

    def event(name, **values):
        record["events"].append({"event": name, "wall_ns": time.time_ns(), **values})
        logfile.write_text(json.dumps(record, indent=2) + "\n")

    if (
        NODES != [(22441, 22451), (22442, 22452)]
        or os.environ["COMPOSE_PROJECT_NAME"] != PROJECT
    ):
        raise ValueError("replication routed outside its registered local ports")
    existing = docker(
        "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"
    ).splitlines()
    if existing:
        raise ValueError(
            "replication project already owns volumes; enrollment requires fresh stores"
        )
    existing = docker(
        "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"
    )
    if existing:
        raise ValueError("replication project already owns containers")
    source = json.loads((payments / "manifest.json").read_text())
    receipt = json.loads((corpus / "sqlite-receipt.json").read_text())
    history = inspect(corpus, receipt)
    if history != json.loads((corpus / "manifest.json").read_text()):
        raise ValueError("archived funding history differs")
    if source["offered_id_sha256"] != SOURCE or (
        source["predecessor_height"],
        history["funding_height"],
    ) != (483, 451):
        raise ValueError("original funds or signed corpus differ")
    if shutil.disk_usage(directory).free < 20 * GIB:
        raise ValueError("host reserve unavailable before setup")
    if docker("image", "inspect", "--format", "{{.Id}}", image) != image_id:
        raise ValueError("immutable release image differs")
    os.environ["SCARY_NODE_IMAGE"] = image
    event(
        "fresh_project_verified",
        funding_history=history["sha256_of_ordered_wire_digests"],
        source_ids=SOURCE,
        host_free_bytes=shutil.disk_usage(directory).free,
    )
    start_references()
    event("independent_references_ready")

    def record_redial(height):
        event("stock_setup_legacy_redial", height=height)

    seed(corpus, receipt, 449, on_redial=record_redial)
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
    wait(
        "three independent native data peers",
        lambda: status()["mining"]["connected_data_peers"] >= 3,
        seconds=180,
    )
    if status()["release"]["executable_sha256"] != RELEASE:
        raise ValueError("running entrant executable differs")
    for height in (450, 451, *range(455, 484, 4)):
        seed(corpus, receipt, height, entrant=True)
        if height == 451:
            checks = status()["mining"]["validation_window"]
            if not any(row["hash"] == receipt["funding_block"] for row in checks):
                raise ValueError("entrant missed original payment funding")
        event("setup_selected", height=height, hash=sv("getbestblockhash"))
    base = prepare_funding(payments)
    if base["hash"] != FUNDING:
        raise ValueError("common split funding differs")
    services = topology()
    for name, info in services.items():
        for mount in info["mounts"]:
            if mount["type"] == "volume" and not mount["volume"].startswith(
                PROJECT + "_"
            ):
                raise ValueError(f"{name} uses a volume outside the fresh project")
    stock = json.loads(docker("inspect", compose("ps", "-q", "svref")))[0]
    flags = stock["Args"]
    if not {
        "-checkmempool=0",
        "-checkblockindex=0",
        "-excessiveblocksize=4000000000",
    } <= set(flags):
        raise ValueError("reference settings differ from registration")
    if services["scary-node"]["image_id"] != image_id:
        raise ValueError("entrant image ID differs")
    if any(rpc(n, "getblockchaininfo")["bestblockhash"] != FUNDING for n in NODES):
        raise ValueError("stock funding tips differ")
    record.update(
        topology=services,
        stock_arguments=flags,
        funding=base,
        executable_sha256=RELEASE,
        source_ids_sha256=SOURCE,
    )
    event("four_stores_ready", selected_height=484, selected_hash=FUNDING)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("history", type=Path)
    parser.add_argument("payments", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--image", default=IMAGE, help="locally built Linux release image"
    )
    parser.add_argument(
        "--image-id",
        default=IMAGE_ID,
        help="full immutable local image ID; the release ELF must match independently",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.payments,
                args.output,
                setup=lambda d: setup(
                    args.history, args.payments, d, args.image, args.image_id
                ),
            ),
            indent=2,
        )
    )
