"""Initialize separate independent stores, replay the common signed funding and run the inventory cohort."""

import argparse
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from sustained_profiles import (
    PROJECTS,
    SUBTREE_SETTINGS,
    WITNESSED_ARMS,
    ENDORSEMENT,
    ENDORSED_HASH,
    ENDORSED_SHA,
    ORIGINAL_IDS,
    REORDERED_IDS,
    REORDERED_ARMS,
    batch_order,
    profile,
)

os.environ.setdefault("SCARY_SUSTAINED_PROFILE", "production")
ARM = profile()
if ARM not in ("production", *WITNESSED_ARMS):
    raise ValueError("choose a registered inventory profile")
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
if ARM == "cold":
    IMAGE = IMAGE_ID = (
        "sha256:12e959a8368b5f4cc561eb754d1588e62f6a5f68bbc38d238174eaa40b8b827a"
    )
elif ARM == "release-reordered":
    IMAGE = IMAGE_ID = (
        "sha256:0da9e6af62129fa7385f78ddf190780c3975598c49211b3fbb838837d9ca2d6f"
    )
elif ARM == "buffered-reordered":
    IMAGE = IMAGE_ID = (
        "sha256:0e61b92676c4b8b52065fc9e2b5e6bbbec0f8cc717c9ddde6bf6c641be6f0c55"
    )
FUNDING = "3fb3a456448fd6d91801a35c04112ffab3f30dc0a6a14e426effff4f91660ba9"
SOURCE = "243170edb3df9bea289c32207732d988f6e4b92f67b17634a72e709ae8b735a2"
PROJECT, SHIFT = PROJECTS[ARM]


def setup(
    corpus, payments, directory, image=IMAGE, image_id=IMAGE_ID, resume_genesis=False
):
    record = {"registration": REGISTRATION, "project": PROJECT, "events": []}
    logfile = directory / "setup.json"

    def event(name, **values):
        record["events"].append({"event": name, "wall_ns": time.time_ns(), **values})
        logfile.write_text(json.dumps(record, indent=2) + "\n")

    if (
        NODES != [(19441 + SHIFT, 19451 + SHIFT), (19442 + SHIFT, 19452 + SHIFT)]
        or os.environ["COMPOSE_PROJECT_NAME"] != PROJECT
    ):
        raise ValueError("replication routed outside its registered local ports")
    existing = docker(
        "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"
    ).splitlines()
    if existing and not resume_genesis:
        raise ValueError(
            "replication project already owns volumes; enrollment requires fresh stores"
        )
    existing = docker(
        "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"
    )
    if existing and not resume_genesis:
        raise ValueError("replication project already owns containers")
    if resume_genesis:
        previous = json.loads(logfile.read_text())
        if (
            previous["registration"] != REGISTRATION
            or previous["project"] != PROJECT
            or [e["event"] for e in previous["events"]] != ["fresh_project_verified"]
            or int(sv("getblockcount")) != 0
            or any(rpc(n, "getblockchaininfo")["blocks"] != 0 for n in NODES)
        ):
            raise ValueError("resume requires the original new stores still at genesis")
        record = previous
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
    if ARM in WITNESSED_ARMS:
        host_free = shutil.disk_usage(directory).free
        vm_free = (
            int(
                docker(
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network",
                    "none",
                    "--read-only",
                    "--entrypoint",
                    "df",
                    "nginx:1.27.4",
                    "-Pk",
                    "/",
                )
                .splitlines()[-1]
                .split()[3]
            )
            * 1024
        )
        # The inventory control used approximately 25 GiB of allocated storage.
        if host_free < 45 * GIB or vm_free < 37 * GIB:
            raise ValueError(
                f"prospective capacity unavailable: host {host_free} bytes, Docker {vm_free} bytes; "
                "reserve plus prior four-store allocation is 45 GiB host / 37 GiB Docker"
            )
    if shutil.disk_usage(directory).free < 20 * GIB:
        raise ValueError("host reserve unavailable before setup")
    if docker("image", "inspect", "--format", "{{.Id}}", image) != image_id:
        raise ValueError("immutable release image differs")
    os.environ["SCARY_NODE_IMAGE"] = image
    event(
        "resumed_fresh_genesis_setup_after_stock_rpc_warmup"
        if resume_genesis
        else "fresh_project_verified",
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
    entrant_status = status()
    if entrant_status["release"]["executable_sha256"] != RELEASE:
        raise ValueError("running entrant executable differs")
    if ARM == "buffered-reordered":
        settings = {k: entrant_status["peer"][k] for k in SUBTREE_SETTINGS}
        if settings != SUBTREE_SETTINGS:
            raise ValueError("running subtree fetch settings differ from registration")
        record["subtree_settings"] = settings
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
    if ARM in WITNESSED_ARMS:
        digest = hashlib.sha256()
        with (payments / "ids.bin").open("rb") as ids:
            for batch in batch_order(ARM):
                ids.seek(batch * 512 * 32)
                data = ids.read(512 * 32)
                if len(data) != 512 * 32:
                    raise ValueError("scheduled input digest is incomplete")
                digest.update(data)
        expected = REORDERED_IDS if ARM in REORDERED_ARMS else ORIGINAL_IDS
        if digest.hexdigest() != expected:
            raise ValueError("offered inventory order differs from registration")
        record.update(arm=ARM, scheduled_order_id_sha256=expected)
    if ARM == "endorsed":
        if hashlib.sha256(ENDORSEMENT).hexdigest() != ENDORSED_SHA:
            raise ValueError("funding successor wire differs")
        if sv("submitblock", ENDORSEMENT.hex()) is not None:
            raise ValueError("stock SV refused registered ordinary successor")

        def every_peer():
            return (
                sv("getbestblockhash") == ENDORSED_HASH
                and all(
                    rpc(n, "getblockchaininfo")["bestblockhash"] == ENDORSED_HASH
                    for n in NODES
                )
                and status()["mining"]["tip_hash"] == ENDORSED_HASH
            )

        wait(
            "all peers selected the initial funding successor", every_peer, seconds=180
        )
        base = {"height": 485, "hash": ENDORSED_HASH, "wire_sha256": ENDORSED_SHA}
        record["endorsement_hex"] = ENDORSEMENT.hex()
    event(
        "four_stores_ready", selected_height=base["height"], selected_hash=base["hash"]
    )
    return base


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
    parser.add_argument(
        "--resume-genesis-setup",
        action="store_true",
        help="resume an enrolled but untimed setup only while all reference histories are still at genesis",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.payments,
                args.output,
                resume_genesis=args.resume_genesis_setup,
                setup=lambda d: setup(
                    args.history,
                    args.payments,
                    d,
                    args.image,
                    args.image_id,
                    args.resume_genesis_setup,
                ),
            ),
            indent=2,
        )
    )
