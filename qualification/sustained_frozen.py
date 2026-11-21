"""Read the sealed direct-wallet project's two SQLite tips and signed-state checkpoint without starting a node."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

import msgpack

PROJECT = "scary-node-throughput"
ROLES = ("sv", "tn1", "tn2", "scary")


def sha(data):
    return hashlib.sha256(data).digest()


def docker(*args):
    return subprocess.check_output(["docker", *args])


def paused_data_mount(role, destination):
    info = json.loads(docker("inspect", f"{PROJECT}-{role}-1"))[0]
    labels = info["Config"]["Labels"]
    if (
        not info["State"]["Paused"]
        or labels.get("com.docker.compose.project") != PROJECT
        or labels.get("com.docker.compose.service") != role
    ):
        raise ValueError("inspected container is outside the frozen enrollment")
    volume = next(m["Name"] for m in info["Mounts"] if m["Destination"] == destination)
    if not volume.startswith(PROJECT + "_"):
        raise ValueError("data volume is outside the frozen project")
    return volume


def inspect_stores(receipt, handoff):
    end = json.loads(gzip.decompress(receipt.read_bytes()))
    marker = json.loads(handoff.read_text())
    height, h = end["completion"]["final_height"], end["completion"]["final_hash"]
    last = end["samples"][-1]
    expected = (height, h)
    for role in ROLES:
        if (last[role]["height"], last[role]["hash"]) != expected:
            raise ValueError("last independently selected parents differ")
    measured = {
        role: {key: last[role][key] for key in ("height", "hash", "read_ns")}
        for role in ROLES
    }
    ingress = json.loads(docker("inspect", f"{PROJECT}-ingress-1"))[0]
    ports = {
        p: [v["HostPort"] for v in target]
        for p, target in ingress["NetworkSettings"]["Ports"].items()
        if target
    }
    published_ports = {port for bound in ports.values() for port in bound}
    if not {"21441", "21442", "21480"} <= published_ports:
        raise ValueError("native endpoints differ from enrolled project")
    result = {
        "registration": end["registration"],
        "recorded_wall_ns": time.time_ns(),
        "direct_wallet_receipt_sha256": hashlib.sha256(
            receipt.read_bytes()
        ).hexdigest(),
        "original_quiescence_sha256": hashlib.sha256(handoff.read_bytes()).hexdigest(),
        "original_post_window_query_ports": {
            "tn1": 19441,
            "tn2": 19442,
            "scary": 19480,
        },
        "excluded_optional_fields": [
            "post_window_selected_tips.tn1",
            "post_window_selected_tips.tn2",
            "post_window_selected_tips.scary",
            "post_window_entrant_observed_above_audited_prefix",
        ],
        "enrolled_project": PROJECT,
        "enrolled_ingress_ports": ports,
        "sampler_last_clock_ns": last["elapsed_ns"],
        "last_scheduled_sample": measured,
        "stock_post_window_tip": marker["post_window_selected_tips"]["sv"],
        "frozen_stores": {},
    }
    if (
        result["stock_post_window_tip"]["height"],
        result["stock_post_window_tip"]["hash"],
    ) != expected:
        raise ValueError("post-window stock SV tip differs")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name in ("tn1", "tn2"):
            volume = paused_data_mount(name, "/app/data")
            local = root / name
            local.mkdir()
            for member in ("blockchain.db", "blockchain.db-wal", "blockchain.db-shm"):
                docker(
                    "cp", f"{PROJECT}-{name}-1:/app/data/{member}", str(local / member)
                )
            with sqlite3.connect(
                f"file:{local / 'blockchain.db'}?mode=ro", uri=True
            ) as db:
                row = db.execute(
                    "SELECT height,hash,invalid FROM blocks WHERE on_main_chain=1 ORDER BY height DESC LIMIT 1"
                ).fetchone()
            if row is None or (row[0], row[1][::-1].hex()) != expected or row[2]:
                raise ValueError("frozen Teranode main chain differs")
            result["frozen_stores"][name] = {
                "height": row[0],
                "hash": row[1][::-1].hex(),
                "invalid": bool(row[2]),
                "volume": volume,
            }
        volume = paused_data_mount("scary-node", "/data")
        path = root / "witness.state"
        docker("cp", f"{PROJECT}-scary-node-1:/data/regtest/witness.state", str(path))
        size = path.stat().st_size
        checksum = hashlib.sha256()
        with path.open("rb") as f:
            if f.read(8) != b"SCARYWS1":
                raise ValueError("unknown checkpoint")
            remaining = size - 40
            while remaining:
                data = f.read(min(1024 * 1024, remaining))
                if not data:
                    raise ValueError("truncated checkpoint")
                remaining -= len(data)
                checksum.update(data)
            if checksum.digest() != f.read(32):
                raise ValueError("checkpoint payload checksum differs")
            f.seek(8)
            data = msgpack.Unpacker(f, raw=False, strict_map_key=False)
            selected, header = None, None
            for _ in range(data.read_map_header()):
                field = data.unpack()
                if field == "height":
                    selected = data.unpack()
                elif field == "headers":
                    header = data.unpack()[-1]
                    break
                else:
                    data.skip()
        if (
            not header
            or len(header) != 80
            or (selected, sha(sha(header))[::-1].hex()) != expected
        ):
            raise ValueError("frozen Scary checkpoint differs")
        result["frozen_stores"]["scary"] = {
            "height": selected,
            "hash": h,
            "header_hex": header.hex(),
            "volume": volume,
            "checkpoint_bytes": size,
            "verified_checkpoint_payload_sha256": checksum.hexdigest(),
        }
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("handoff", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect_stores(args.receipt, args.handoff), indent=2))
