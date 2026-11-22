"""Build the frozen checkpoint serializer with the registered paired test and verify each sample."""

import argparse
import datetime
import gzip
import hashlib
import json
import platform
import shutil
import statistics
import subprocess
import tarfile
import tempfile
from pathlib import Path

from run_cohort import session_lock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "af9b605ad8da96d041a540111d46a7c0105209f7"
REGISTRATION = "d8def6755a79cdb98b7f993bfafaff5cec3ba778"
EXPECTED = "a46c75038049c8194ce235e030bd0c515f955aa8036d88b62256232a01a72a82"
SIZE = 316_588_547
HARNESS = ROOT / "qualification/checkpoint_profile.rs"
TEST = "mining::store::checkpoint_profile::profile"
PHASES = (
    "open_ns",
    "encode_hash_write_ns",
    "checksum_flush_ns",
    "file_sync_ns",
    "rename_ns",
    "directory_sync_ns",
)


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def build(directory):
    directory.mkdir(exist_ok=False)
    with tempfile.TemporaryDirectory() as name:
        context = Path(name)
        archive = subprocess.Popen(
            [
                "git",
                "-C",
                ROOT,
                "archive",
                SOURCE,
                "--",
                "Cargo.toml",
                "Cargo.lock",
                "src",
                "native",
                "scripts/build-bdk.sh",
                "Dockerfile",
                ".dockerignore",
            ],
            stdout=subprocess.PIPE,
        )
        with archive, tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
            tar.extractall(context, filter="data")
        if archive.returncode:
            raise RuntimeError("failed to export the pinned source")
        store = context / "src/mining/store.rs"
        with store.open("a") as f:
            f.write(
                '\n#[cfg(test)] mod checkpoint_profile { include!("checkpoint_profile.rs"); }\n'
            )
        shutil.copyfile(HARNESS, context / "src/mining/checkpoint_profile.rs")
        dockerfile = context / "Dockerfile"
        text = dockerfile.read_text().replace(
            "type=cache,target=/build/target", "type=tmpfs,target=/build/target"
        )
        original = "cargo build --locked --release && install -m 755 target/release/scary-node /usr/local/bin/scary-node"
        if text.count(original) != 1:
            raise ValueError("unexpected frozen build recipe")
        text = text.replace(
            original,
            "cargo test --locked --release --bin scary-node --no-run --message-format=json-render-diagnostics > /tmp/build.json && "
            "python3 -c \"import json,shutil; rows=[json.loads(s) for s in open('/tmp/build.json')]; "
            "exe,=[r['executable'] for r in rows if r.get('executable') and r['profile']['test']]; "
            "shutil.copy2(exe,'/usr/local/bin/scary-node')\" && rustc -Vv >/tmp/rustc-identity",
        )
        text += (
            "\nCOPY --from=build /tmp/rustc-identity /usr/local/share/rustc-identity\n"
        )
        dockerfile.write_text(text)
        with (directory / "build.log").open("xb") as log:
            subprocess.run(
                ["docker", "build", "--iidfile", directory / "image.id", context],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
    image = (directory / "image.id").read_text().strip()
    if (
        TEST + ": test"
        not in command(
            "docker", "run", "--rm", "--network", "none", image, "--list"
        ).splitlines()
    ):
        raise RuntimeError("compiled test missing")
    report = {
        "source_commit": SOURCE,
        "registration": REGISTRATION,
        "harness_sha256": hashlib.sha256(HARNESS.read_bytes()).hexdigest(),
        "image_id": image,
        "binary_sha256": command(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "sha256sum",
            image,
            "/usr/local/bin/scary-node",
        ).split()[0],
        "rustc": command(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "cat",
            image,
            "/usr/local/share/rustc-identity",
        ).splitlines(),
    }
    (directory / "build.json").write_text(json.dumps(report, indent=2) + "\n")


def profile_summary(rows):
    warm, *samples = rows
    if warm["event"] != "warmup" or len(samples) != 48:
        raise ValueError("incomplete fixed 24-pair cohort")
    pairs = []
    for i in range(24):
        a, b = samples[2 * i : 2 * i + 2]
        if (
            [a["pair"], b["pair"]] != [i, i]
            or [a["position"], b["position"]] != [0, 1]
            or [a["save"]["writer"], b["save"]["writer"]]
            != (
                ["buffered_hash", "existing"]
                if i % 2
                else ["existing", "buffered_hash"]
            )
        ):
            raise ValueError("recorded pair order differs from registration")
        for sample in (a, b):
            save = sample["save"]
            if (
                sample["event"] != "sample"
                or set(save) != {"writer", "total_ns", *PHASES}
                or any(
                    type(save[k]) is not int or save[k] < 0
                    for k in (*PHASES, "total_ns")
                )
                or not save["total_ns"]
                or sum(save[k] for k in PHASES) != save["total_ns"]
            ):
                raise ValueError("phase total mismatch or invalid sample")
        pairs.append({r["save"]["writer"]: r["save"] for r in (a, b)})

    def stats(vals):
        return {
            "minimum": min(vals),
            "median": statistics.median(vals),
            "maximum": max(vals),
        }

    a = [p["existing"]["total_ns"] for p in pairs]
    b = [p["buffered_hash"]["total_ns"] for p in pairs]
    return {
        "pairs": len(pairs),
        "b_minus_a_ms": stats([(y - x) / 1e6 for x, y in zip(a, b, strict=True)]),
        "b_over_a": stats([y / x for x, y in zip(a, b, strict=True)]),
        "buffered_faster_pairs": sum(y < x for x, y in zip(a, b, strict=True)),
        "existing_faster_pairs": sum(x < y for x, y in zip(a, b, strict=True)),
        "by_order": {
            label: {
                "pairs": 12,
                "buffered_faster_pairs": sum(b[i] < a[i] for i in indices),
                "existing_faster_pairs": sum(a[i] < b[i] for i in indices),
                "b_over_a": stats([b[i] / a[i] for i in indices]),
            }
            for label, indices in [
                ("existing_then_buffered", range(0, 24, 2)),
                ("buffered_then_existing", range(1, 24, 2)),
            ]
        },
        "phase_ms": {
            writer: {
                key: stats([p[writer][key] / 1e6 for p in pairs])
                for key in pairs[0][writer]
                if key.endswith("_ns")
            }
            for writer in ("existing", "buffered_hash")
        },
    }


def read_json(path):
    data = path.read_bytes()
    return json.loads(gzip.decompress(data) if data.startswith(b"\x1f\x8b") else data)


def identity(wire):
    if len(wire) != SIZE or wire[:8] != b"SCARYWS1":
        raise ValueError("checkpoint framing or length differs")
    payload = memoryview(wire)[8:-32]
    digest = hashlib.sha256(payload)
    if digest.digest() != wire[-32:]:
        raise ValueError("checkpoint payload checksum differs")
    return {
        "size": len(wire),
        "file_sha256": hashlib.sha256(wire).hexdigest(),
        "payload_sha256": digest.hexdigest(),
    }


def decode(wire):
    import msgpack

    return msgpack.unpackb(
        memoryview(wire)[8:-32], use_list=False, strict_map_key=False
    )


def validate(report):
    if (
        report["schema"] != "scary-node-checkpoint-profile-v1"
        or report["buffer_bytes"] != 8192
    ):
        raise ValueError("unexpected profile format or buffer capacity")
    build = report["build"]
    if (
        build["source_commit"] != SOURCE
        or build["registration"] != REGISTRATION
        or len(bytes.fromhex(build["binary_sha256"])) != 32
        or len(bytes.fromhex(build["harness_sha256"])) != 32
    ):
        raise ValueError("invalid pinned build")
    if (
        report["original"]["payload_sha256"] != EXPECTED
        or report["original"]["size"] != SIZE
    ):
        raise ValueError("source differs from the registered checkpoint")
    typed = report["typed"]
    warm = report["observations"][0]
    if (
        warm["original_payload_sha256"] != EXPECTED
        or warm["bytes"] != SIZE
        or typed["size"] != SIZE
        or warm["typed_file_sha256"] != typed["file_sha256"]
        or warm["payload_bytes"] != SIZE - 40
        or warm["buffered_hash_calls"] <= 0
        or warm["existing_hash_calls"] <= 0
        or report["typed_matches_original_semantics"] is not True
        or report["archived_wire_matches_both_writers"] is not True
        or [v["writer"] for v in warm["saves"]] != ["existing", "buffered_hash"]
    ):
        raise ValueError("state and both writers must be verified")
    result = profile_summary(report["observations"])
    if report["summary"] != result:
        raise ValueError("summary differs from the paired observations")
    return result


def platform_identity(image, volume, out):
    system = platform.uname()
    mac = system.system == "Darwin"
    docker = json.loads(command("docker", "info", "--format", "{{json .}}"))
    # Restrict the receipt to non-identifying host characteristics.
    cpu = (
        command("sysctl", "-n", "machdep.cpu.brand_string")
        if mac
        else command("lscpu", "--parse=MODELNAME").splitlines()[-1]
    )
    memory = (
        int(command("sysctl", "-n", "hw.memsize"))
        if mac
        else int(
            next(
                s.split()[1]
                for s in Path("/proc/meminfo").read_text().splitlines()
                if s.startswith("MemTotal:")
            )
        )
        * 1024
    )
    raw = command(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--entrypoint",
        "df",
        "--mount",
        f"type=volume,source={volume},target=/scratch,readonly",
        image,
        "-P",
        "-T",
        "-B1",
        "/scratch",
    ).splitlines()
    _, fs, total, _, available, _, _ = raw[-1].split()
    env = {
        "host_system": system.system,
        "host_machine": system.machine,
        "host_kernel": system.release,
        "host_os": platform.mac_ver()[0]
        if mac
        else platform.freedesktop_os_release().get("PRETTY_NAME"),
        "host_cpu": cpu,
        "host_memory_bytes": memory,
        "docker": {
            "version": docker["ServerVersion"],
            "kernel": docker["KernelVersion"],
            "arch": docker["Architecture"],
            "cpus": docker["NCPU"],
            "memory_bytes": docker["MemTotal"],
            "driver": docker["Driver"],
            "volume_filesystem": fs,
            "volume_capacity_bytes": int(total),
            "volume_available_before_bytes": int(available),
        },
        "host_available_before_bytes": shutil.disk_usage(out).free,
    }
    if int(available) < 13 * (2**30) or env["host_available_before_bytes"] < 21 * (
        2**30
    ):
        raise ValueError(
            "insufficient capacity to preserve workload evidence and run the fixture"
        )
    return env


def run(source, build_dir, out, volume):
    if not source.is_file() or source.stat().st_mode & 0o222:
        raise ValueError("provide a protected, read-only copy of the closed checkpoint")
    original = identity(source.read_bytes())
    if original["payload_sha256"] != EXPECTED:
        raise ValueError("source differs from the registered payload")
    build = read_json(build_dir / "build.json")
    image = build["image_id"]
    if (
        build["harness_sha256"] != hashlib.sha256(HARNESS.read_bytes()).hexdigest()
        or build["source_commit"] != SOURCE
        or build["registration"] != REGISTRATION
        or command("docker", "image", "inspect", image, "--format", "{{.Id}}") != image
    ):
        raise ValueError("image or harness differs from the frozen test")
    if volume in command("docker", "volume", "ls", "-q").splitlines():
        raise ValueError("provide a new scratch volume")
    out.mkdir(exist_ok=False)
    if (
        command(
            "docker",
            "volume",
            "create",
            "--label",
            "scary-node.qualification=checkpoint-profile",
            volume,
        )
        != volume
    ):
        raise RuntimeError("scratch volume creation failed")
    # The registration preserves partial results, including the environment and raw test log.
    environment = platform_identity(image, volume, out)
    (out / "platform.json").write_text(json.dumps(environment, indent=2) + "\n")
    started = datetime.datetime.now(datetime.UTC).isoformat()
    with (out / "run.log").open("xb") as log:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--env",
                "SCARY_PROFILE_SOURCE=/input/witness.state",
                "--env",
                "SCARY_PROFILE_SCRATCH=/scratch",
                "--mount",
                f"type=bind,source={source},target=/input/witness.state,readonly",
                "--mount",
                f"type=volume,source={volume},target=/scratch",
                image,
                "--ignored",
                "--exact",
                TEST,
                "--nocapture",
                "--test-threads=1",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    finished = datetime.datetime.now(datetime.UTC).isoformat()
    log = (out / "run.log").read_text()
    if "test result: ok. 1 passed" not in log:
        raise ValueError("full ignored test did not pass")
    observations = [
        json.loads(line.partition("CHECKPOINT_PROFILE ")[2])
        for line in log.splitlines()
        if "CHECKPOINT_PROFILE " in line
    ]
    typed_wire = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--mount",
            f"type=volume,source={volume},target=/scratch,readonly",
            "--entrypoint",
            "cat",
            image,
            "/scratch/typed.reference",
        ]
    )
    typed = identity(typed_wire)
    typed_state = decode(typed_wire)
    del typed_wire
    original_wire = source.read_bytes()
    if identity(original_wire) != original or decode(original_wire) != typed_state:
        raise ValueError("archived and reopened complete nested states differ")
    report = {
        "schema": "scary-node-checkpoint-profile-v1",
        "started_at": started,
        "finished_at": finished,
        "original": original,
        "typed": typed,
        "typed_matches_original_semantics": True,
        "archived_wire_matches_both_writers": True,
        "build": build,
        "environment": environment,
        "buffer_bytes": 8192,
        "observations": observations,
        "summary": profile_summary(observations),
    }
    validate(report)
    (out / "result.json.gz").write_bytes(
        gzip.compress(
            (json.dumps(report, sort_keys=True, indent=2) + "\n").encode(), mtime=0
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    subs = p.add_subparsers(dest="mode", required=True)
    b = subs.add_parser("build")
    b.add_argument("directory", type=Path)
    r = subs.add_parser("run")
    r.add_argument("source", type=Path)
    r.add_argument("build_directory", type=Path)
    r.add_argument("output", type=Path)
    r.add_argument("--scratch-volume", required=True)
    v = subs.add_parser("verify")
    v.add_argument("result", type=Path)
    a = p.parse_args()
    if a.mode == "verify":
        print(json.dumps(validate(read_json(a.result)), indent=2))
    elif a.mode == "build":
        with session_lock():
            build(a.directory.resolve())
    else:
        with session_lock():
            run(
                a.source.resolve(),
                a.build_directory.resolve(),
                a.output.resolve(),
                a.scratch_volume,
            )
