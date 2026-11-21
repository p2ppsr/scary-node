"""Boot operator-supplied archived Scary Node state in an isolated, empty Linux RAM filesystem."""

import argparse
import contextlib
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from recover_sustained_chain import read
from run_cohort import session_lock
from verify_receipt import require

HELPER = "nginx:1.27.4@sha256:09369da6b10306312cd908661320086bf87fbae1b6b0c49a1f50ba531fef2eab"
PAYOUT = "76a914751e76e8199196d454941c45d1b3a323f1433bd688ac"
CAPACITY = {"SCARY_MAX_BLOCK_BYTES", "SCARY_MAX_JOURNAL_BYTES", "SCARY_MAX_UNDO_ITEMS"}


def docker(*args, **kwargs):
    return subprocess.check_output(["docker", *args], text=True, **kwargs).strip()


def observation(path):
    value = read(path)
    return value.get("checkpoint", value)


def canonical(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def expected(observation):
    if "restored" in observation:
        # A live-cadence checkpoint has the complete independently observed window.
        return observation["release"], observation["restored"], {}
    end = observation["completion"]
    blocks = {r["hash"]: r for r in observation["blocks"]}
    return (
        observation["release_sha256"],
        {"height": end["final_height"], "tip_hash": end["final_hash"]},
        blocks,
    )


def archive(project, target):
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    name = project + "-scary-node-1"
    c = json.loads(docker("inspect", name))[0]
    require(
        c["State"]["Paused"]
        and c["Config"]["Labels"]["com.docker.compose.project"] == project
        and c["Config"]["Labels"]["com.docker.compose.service"] == "scary-node",
        "capture a frozen enrolled entrant",
    )
    for dest, role in [("/data", "data"), ("/var/lib/scary-state", "leases")]:
        mount = next(m for m in c["Mounts"] if m["Destination"] == dest)
        require(
            mount["Type"] == "volume" and mount["Name"].startswith(project + "_"),
            "unexpected custody mount",
        )
        args = [
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--network",
            "none",
            "--log-driver",
            "none",
            "--pull=never",
            "--mount",
            "type=volume,source=" + mount["Name"] + ",target=/frozen,readonly",
            "--entrypoint",
            "tar",
            HELPER,
            "--numeric-owner",
            "--xattrs",
            "--acls",
            "--sparse",
            "-cf",
            "-",
            "-C",
            "/frozen",
            ".",
        ]
        with (
            subprocess.Popen(args, stdout=subprocess.PIPE) as proc,
            (target / (role + ".tar.gz")).open("xb") as destfile,
            gzip.GzipFile(
                filename="", fileobj=destfile, mode="wb", compresslevel=1, mtime=0
            ) as out,
        ):
            shutil.copyfileobj(proc.stdout, out, 1 << 20)
            require(proc.wait() == 0, "frozen volume export failed")
    # The capture directory is private. Only the public proof should be published.
    return {
        "private_archives": 2,
        "image_id": c["Image"],
        "settings": [v for v in c["Config"]["Env"] if v.partition("=")[0] in CAPACITY],
    }


def extract(cid, uri, dest):
    with contextlib.ExitStack() as stack:
        raw = uri if hasattr(uri, "read") else stack.enter_context(Path(uri).open("rb"))
        source = (
            stack.enter_context(gzip.GzipFile(fileobj=raw))
            if raw.peek(2).startswith(b"\x1f\x8b")
            else raw
        )
        with subprocess.Popen(
            [
                "docker",
                "exec",
                "-i",
                cid,
                "tar",
                "--numeric-owner",
                "--xattrs",
                "--acls",
                "--same-owner",
                "--same-permissions",
                "--delay-directory-restore",
                "-xpf",
                "-",
                "-C",
                dest,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
        ) as proc:
            with proc.stdin as pipe:
                shutil.copyfileobj(source, pipe, 1 << 20)
            require(proc.wait() == 0, "private archive extraction failed")


def boot(image, settings, data, leases, observation_path):
    reference = observation(observation_path)
    release, wanted, measured_blocks = expected(reference)
    args = [
        "create",
        "--pull=never",
        "--name",
        "scary-restored-" + uuid.uuid4().hex[:12],
        "--read-only",
        "--network",
        "none",
        "--log-driver",
        "none",
        "--tmpfs",
        "/data:rw,size=4g",
        "--tmpfs",
        "/var/lib/scary-state:rw,size=16m",
        "--tmpfs",
        "/run/restore:rw,size=1m",
        "--tmpfs",
        "/tmp:rw,size=64m",
    ]
    for setting in settings:
        name, _, value = setting.partition("=")
        require(
            name in CAPACITY and value.isdecimal(),
            "unexpected restore capacity setting",
        )
        args += ["--env", setting]
    args += [
        "--env",
        "XDG_STATE_HOME=/var/lib/scary-state",
        "--entrypoint",
        "/bin/sh",
        image,
        "-ec",
        'while [ ! -e /run/restore/start ]; do sleep .1; done; exec "$@"',
        "saved-state",
        "scary-node",
        "--network",
        "regtest",
        "--listen",
        "/ip4/127.0.0.1/tcp/9905",
        "--api-bind",
        "0.0.0.0:8080",
        "--asset-bind",
        "127.0.0.1:9090",
        "--asset-url",
        "http://127.0.0.1:9090/api/v1",
        "--stratum-bind",
        "127.0.0.1:3333",
        "--payout-script",
        PAYOUT,
        "--data-dir",
        "/data",
    ]
    cid = docker(*args)
    try:
        docker("start", cid)
        extract(cid, data, "/data")
        extract(cid, leases, "/var/lib/scary-state")
        path = "/data/regtest/witness.state"
        wire_sha = docker("exec", cid, "sha256sum", path).split()[0]
        size = int(docker("exec", cid, "stat", "-c", "%s", path))
        docker("exec", cid, "touch", "/run/restore/start")
        deadline = time.monotonic() + 120
        while True:
            try:
                status = json.loads(
                    docker(
                        "run",
                        "--rm",
                        "--read-only",
                        "--network",
                        "container:" + cid,
                        "--log-driver",
                        "none",
                        "--pull=never",
                        "--entrypoint",
                        "curl",
                        HELPER,
                        "-fsS",
                        "--max-time",
                        "3",
                        "http://127.0.0.1:8080/status",
                        stderr=subprocess.DEVNULL,
                    )
                )
                break
            except subprocess.CalledProcessError:
                if (
                    time.monotonic() > deadline
                    or docker("inspect", "--format", "{{.State.Running}}", cid)
                    != "true"
                ):
                    raise
                time.sleep(0.5)
        m = status["mining"]
        require(
            {k: m[k] for k in wanted} == wanted
            and status["release"]["executable_sha256"] == release
            and m["connected_data_peers"] == 0,
            "independent recovery differs from original observation",
        )
        checked = 0
        for row in m["validation_window"]:
            original = measured_blocks.get(row["hash"])
            if original:
                require(
                    (
                        row["height"],
                        row["script_checks_passed"],
                        row["noncoinbase_transactions"],
                    )
                    == (
                        original["height"],
                        original["entrant_script_checks"],
                        original["ordinary_transactions"],
                    ),
                    "retained validation coverage differs",
                )
                checked += 1
        if measured_blocks:
            require(
                checked > 0 and m["tip_hash"] in measured_blocks,
                "archived window has no original witnessed checks",
            )
        return {
            "image_id": docker("inspect", "--format", "{{.Image}}", cid),
            "release_sha256": release,
            "checkpoint_bytes": size,
            "checkpoint_sha256": wire_sha,
            "measured_observation_sha256": hashlib.sha256(
                observation_path.read_bytes()
            ).hexdigest(),
            "network": "isolated regtest with zero connected peers",
            "capacity": sorted(settings),
            "restored": {
                k: m[k]
                for k in (
                    "height",
                    "tip_hash",
                    "coverage_start",
                    "coverage_end",
                    "witnessed_outputs",
                    "window_chainwork",
                    "durable_directive_sequence",
                )
            },
            "measured_record_sha256": canonical(reference),
            "restored_validation_window": m["validation_window"],
            "connected_data_peers": m["connected_data_peers"],
            "retained_window_rows": len(m["validation_window"]),
            "identical_complete_window": "validation_window" in wanted,
            "compared_measured_block_rows": checked,
        }
    finally:
        subprocess.run(
            ["docker", "rm", "-f", cid], check=True, stdout=subprocess.DEVNULL
        )


def verify_boot(proof, original):
    release, wanted, blocks = expected(original)
    window = proof["restored_validation_window"]
    boundary = dict(proof["restored"], validation_window=window)
    require(
        all(boundary[key] == value for key, value in wanted.items()),
        "different recovered observation boundary",
    )
    require(
        proof["measured_record_sha256"] == canonical(original)
        and proof["release_sha256"] == release
        and proof["connected_data_peers"] == 0
        and proof["checkpoint_bytes"] > 0
        and len(bytes.fromhex(proof["checkpoint_sha256"])) == 32
        and len(bytes.fromhex(proof["image_id"].removeprefix("sha256:"))) == 32,
        "image, checkpoint, imported observation or peer count differs",
    )
    require(
        [row["height"] for row in window]
        == list(range(boundary["coverage_start"], boundary["coverage_end"] + 1))
        and window[-1]["hash"] == boundary["tip_hash"]
        and window[-1]["height"] == boundary["height"]
        and len(window) == proof["retained_window_rows"],
        "recovered coverage is not contiguous",
    )
    checked = 0
    for row in window:
        if (block := blocks.get(row["hash"])) is not None:
            require(
                (
                    row["height"],
                    row["script_checks_passed"],
                    row["noncoinbase_transactions"],
                )
                == (
                    block["height"],
                    block["entrant_script_checks"],
                    block["ordinary_transactions"],
                ),
                "observed block checks differ",
            )
            checked += 1
    require(
        checked == proof["compared_measured_block_rows"]
        and (not blocks or checked)
        and proof["identical_complete_window"] == ("validation_window" in wanted),
        "incorrect window accounting",
    )
    return {
        "height": boundary["height"],
        "tip_hash": boundary["tip_hash"],
        "witnessed_outputs": boundary["witnessed_outputs"],
        "retained_window_rows": len(window),
        "matched_measured_blocks": checked,
        "connected_data_peers": 0,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    actions = p.add_subparsers(dest="action", required=True)
    capture = actions.add_parser("capture")
    capture.add_argument("enrolled_project")
    capture.add_argument("new_private_directory", type=Path)
    restore = actions.add_parser("boot")
    restore.add_argument("observation", type=Path)
    restore.add_argument("data_tar", type=Path)
    restore.add_argument("lease_tar", type=Path)
    restore.add_argument("new_public_proof", type=Path)
    restore.add_argument("--image", required=True)
    restore.add_argument("--setting", action="append", default=[])
    check = actions.add_parser("verify")
    check.add_argument("observation", type=Path)
    check.add_argument("proof", type=Path)
    a = p.parse_args()
    if a.action == "verify":
        print(
            json.dumps(verify_boot(read(a.proof), observation(a.observation)), indent=2)
        )
        raise SystemExit(0)
    with session_lock():
        if a.action == "capture":
            os.umask(0o077)
            print(
                json.dumps(
                    archive(a.enrolled_project, a.new_private_directory), indent=2
                )
            )
        else:
            if a.new_public_proof.exists():
                p.error("choose a new proof path")
            r = boot(a.image, a.setting, a.data_tar, a.lease_tar, a.observation)
            verify_boot(r, observation(a.observation))
            tmp = a.new_public_proof.with_suffix(".next")
            tmp.write_text(json.dumps(r, indent=2) + "\n")
            tmp.replace(a.new_public_proof)
            print(json.dumps(r, indent=2))
