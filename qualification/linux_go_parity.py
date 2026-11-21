"""Run the distributed and rebuilt ARM64 Go archives inside the frozen Scary Node release image."""

import argparse
import contextlib
import json
import subprocess
from pathlib import Path

from go_native_parity import ROLES, collect, sha, verify
from shadow_bdk import replay
from shadow_first_candidate import load, pack
from shadow_native import require
from shadow_native_warmup import verify as verify_warmup

EVIDENCE = Path(__file__).resolve().parents[1] / "docs/evidence"


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def capture(corpus, warmup, release, manifest, folder, image):
    image_id = docker("image", "inspect", image, "--format", "{{.Id}}")
    require(
        image_id == release["image_id"],
        "image differs from the frozen release manifest",
    )
    container = docker(
        "run",
        "--detach",
        "--network",
        "none",
        "--read-only",
        "--platform",
        "linux/arm64",
        "--cpus",
        "2",
        "--memory",
        "1g",
        "--memory-swap",
        "1g",
        "--tmpfs",
        "/tmp:rw,size=67108864",
        "--mount",
        f"type=bind,source={folder},target=/oracles,readonly",
        "--entrypoint",
        "/bin/sleep",
        image_id,
        "3600",
    )
    with contextlib.ExitStack() as stack:
        stack.callback(docker, "rm", "-f", container)

        def run(*args):
            return docker("exec", container, *args)

        state = json.loads(docker("inspect", container))[0]
        runtime = {
            "platform": docker(
                "image", "inspect", image_id, "--format", "{{.Os}}/{{.Architecture}}"
            ),
            "machine": run("uname", "-m"),
            "scary_version": run("/usr/local/bin/scary-node", "--version"),
            "image_id": state["Image"],
            "release_elf_sha256": run("sha256sum", "/usr/local/bin/scary-node").split()[
                0
            ],
            "network": state["HostConfig"]["NetworkMode"],
            "read_only": state["HostConfig"]["ReadonlyRootfs"],
            "oracle_sha256": {},
            "linked": {},
        }
        # Resolve all identities inside the shipping filesystem before checking any cases.
        for role in ROLES:
            for tool in ("mainnet", "corpus"):
                name = role + "-" + tool
                runtime["oracle_sha256"][name] = run(
                    "sha256sum", "/oracles/" + name
                ).split()[0]
            runtime["linked"][role] = json.loads(run("/oracles/" + role + "-mainnet"))
        verify_runtime(runtime, release, manifest)

        def command(binary):
            return ["docker", "exec", "-i", container, "/oracles/" + binary.name]

        rows = collect(corpus, manifest, folder, command)
        payment = {}
        for role in ROLES:
            binary = folder / (role + "-mainnet")
            payment[role] = replay(warmup, binary, role, command(binary))
    return {"runtime": runtime, "rows": rows, "confirmed_warmup": payment}


def verify_runtime(runtime, release, manifest):
    require(
        runtime["platform"] == release["platform"] == "linux/arm64"
        and manifest["platform"] == "linux_aarch64"
        and runtime["machine"] == "aarch64",
        "unexpected runtime or archive architecture",
    )
    require(
        runtime["image_id"] == release["image_id"]
        and runtime["release_elf_sha256"] == release["release"]["executable_sha256"],
        "actual runtime differs from registered Linux release",
    )
    require(
        runtime["read_only"] and runtime["network"] == "none",
        "dynamic oracle trial lacks isolation",
    )
    require(
        runtime["scary_version"].split()[-1] == release["release"]["version"]
        and manifest["bdk_commit"] == release["interpreter"]["bdk_commit"]
        and manifest["sv_node_commit"] == release["interpreter"]["sv_node_commit"],
        "shipping runtime or interpreter source pin differs",
    )
    for role in ROLES:
        variant = manifest["variants"][role]
        require(
            runtime["linked"][role] == variant["linked"],
            "runtime-reported native identity differs",
        )
        for tool in ("mainnet", "corpus"):
            require(
                runtime["oracle_sha256"][role + "-" + tool]
                == variant[tool + "_oracle_sha256"],
                "runtime binary differs from archive manifest",
            )


def verify_linux(record, corpus, warmup, release, manifest, files):
    require(
        record["release_sha256"] == sha(files.release)
        and record["warmup_sha256"] == sha(files.warmup),
        "Linux release manifest or confirmed source fixture differs",
    )
    verify_runtime(record["runtime"], release, manifest)
    distributed = json.loads(files.distributed_identity.read_text())["archives"][
        "libGoBDK_linux_aarch64.a"
    ]
    observed = record["runtime"]["linked"]["distributed"]
    require(
        distributed["archive_sha256"]
        == manifest["variants"]["distributed"]["native_archive_sha256"]
        and all(
            distributed["symbols"][symbol]["value"] == observed[key]
            for key, symbol in (
                ("source", "CGO_SOURCE_GIT_COMMIT_HASH"),
                ("sv_source", "CGO_BSV_GIT_COMMIT_HASH"),
                ("bdk_version", "CGO_BDK_VERSION_STRING"),
                ("sv_version", "CGO_BSV_VERSION_STRING"),
            )
        ),
        "dynamic Linux identity differs from independently inspected upstream archive",
    )
    result = verify(record, corpus, manifest, sha(files.corpus), sha(files.manifest))
    spend_height = verify_warmup(warmup)["spend_height"]
    for role in ROLES:
        payment = record["confirmed_warmup"][role]
        change = payment["altered_funding_amount"]
        require(
            warmup["kind"] == payment["kind"] == "confirmed-warmup-spend"
            and payment["parent_height"] + 1
            == payment["containing_height"]
            == spend_height
            and payment["native_variant"] == role
            and payment["go_oracle_sha256"]
            == record["runtime"]["oracle_sha256"][role + "-mainnet"]
            and payment["build"] == record["runtime"]["linked"][role]
            and payment["ordinary_consensus_accepted"]
            == payment["ordinary_policy_accepted"]
            == payment["transactions"]
            == 1
            and payment["ordinary_rejections"] == []
            and payment["confiscations_requiring_signed_authorization"] == 0
            and payment["ordinary_policy_complete"] is True
            and payment["ordinary_consensus_complete"] is True
            and change["policy"] is False
            and change["consensus"] is False
            and all(
                "signature" in change[c + "_error"].lower()
                for c in ("policy", "consensus")
            ),
            "mainnet signed spending control differs",
        )
    result["original_live_node_sha256"] = result.pop("release_sha256")
    result["runtime"] = record["runtime"]
    result["confirmed_warmup_height"] = spend_height
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "manifest", type=Path, help="manifest produced by build-go-linux-oracles.sh"
    )
    p.add_argument("receipt", type=Path)
    p.add_argument("--capture", type=Path, help="four compiled ARM64 binaries")
    p.add_argument("--image", help="local reference to the frozen image")
    p.add_argument(
        "--release", type=Path, default=EVIDENCE / "chronicle-waiting-linux-build.json"
    )
    p.add_argument(
        "--corpus",
        type=Path,
        default=EVIDENCE / "chronicle-waiting-entrant-corpus.json.gz",
    )
    p.add_argument(
        "--warmup",
        type=Path,
        default=EVIDENCE / "chronicle-shadow-native-fee-warmup.json.gz",
    )
    p.add_argument(
        "--distributed-identity",
        type=Path,
        default=EVIDENCE / "chronicle-go-native-linux-identities.json",
    )
    a = p.parse_args()
    corpus = load(a.corpus)
    warmup = load(a.warmup)
    release = json.loads(a.release.read_text())
    m = json.loads(a.manifest.read_text())
    if a.capture:
        record = capture(
            corpus,
            warmup,
            release,
            m,
            a.capture.resolve(),
            a.image or release["image_id"],
        )
        record.update(
            schema=1,
            corpus_sha256=sha(a.corpus),
            manifest_sha256=sha(a.manifest),
            release_sha256=sha(a.release),
            warmup_sha256=sha(a.warmup),
        )
        a.receipt.write_bytes(pack(record))
    else:
        record = load(a.receipt)
    summary = verify_linux(record, corpus, warmup, release, m, a)
    print(json.dumps(summary, indent=2))
    raise SystemExit(1 if summary["disagreements_with_live_stock"] else 0)
