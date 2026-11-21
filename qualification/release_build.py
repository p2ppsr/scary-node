"""Build a committed source tree and identify the linked release through its running node."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
BUILD_INPUTS = [
    ".dockerignore",
    "Dockerfile",
    "Cargo.toml",
    "Cargo.lock",
    "native",
    "scripts/build-bdk.sh",
    "src",
]


def output(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def build(directory, target):
    options = ["--target", target] if target else []
    iid = directory / (target or "release")
    subprocess.run(
        [
            "docker",
            "build",
            *options,
            "--iidfile",
            str(iid),
            str(directory / "context"),
        ],
        check=True,
    )
    return iid.read_text().strip()


def runtime_status(image):
    container = output(
        "docker",
        "run",
        "-d",
        "--network",
        "none",
        image,
        "--network",
        "regtest",
        "--sv-peer",
        "127.0.0.1:1",
    )
    try:
        digest = output(
            "docker", "exec", container, "sha256sum", "/usr/local/bin/scary-node"
        ).split()[0]
        # The operator endpoint remains entirely inside the isolated container.
        request = "exec 3<>/dev/tcp/127.0.0.1/8080 && printf 'GET /status HTTP/1.0\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n' >&3 && cat <&3"
        for _ in range(100):
            reply = subprocess.run(
                ["docker", "exec", container, "bash", "-c", request],
                capture_output=True,
            )
            if reply.returncode == 0:
                headers, payload = reply.stdout.split(b"\r\n\r\n", 1)
                if b" 200 " not in headers.splitlines()[0]:
                    raise RuntimeError("node did not return a successful status")
                status = json.loads(payload)
                if status["release"]["executable_sha256"] != digest:
                    raise ValueError(
                        "running node and independently hashed executable disagree"
                    )
                return status["release"], status["mining"]["interpreter"]
            time.sleep(0.1)
        raise TimeoutError("isolated node did not expose its operator status")
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], check=True, stdout=subprocess.DEVNULL
        )


def run(revision, directory):
    commit = output(
        "git", "rev-parse", "--verify", "--end-of-options", revision + "^{commit}"
    )
    tree = output("git", "rev-parse", commit + "^{tree}")
    archive = directory / "source.tar"
    subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            "-o",
            str(archive),
            commit,
            "--",
            *BUILD_INPUTS,
        ],
        check=True,
        cwd=ROOT,
    )
    with tarfile.open(archive) as inputs:
        inputs.extractall(directory / "context", filter="data")
    compiler = build(directory, "build")
    release_image = build(directory, None)
    rust = output(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "rustc",
        compiler,
        "-vV",
    ).splitlines()
    cargo = output(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "cargo",
        compiler,
        "-V",
    )
    platform = output(
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Os}}/{{.Architecture}}",
        release_image,
    )
    release, interpreter = runtime_status(release_image)
    for short, full in [
        ("linked_bdk_commit", "bdk_commit"),
        ("linked_sv_commit", "sv_node_commit"),
    ]:
        reported = interpreter[short]
        if len(reported) < 7 or not interpreter[full].startswith(reported):
            raise ValueError("linked interpreter does not match its pinned revision")
    archive_digest = output(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "sha256sum",
        compiler,
        "/build/.native/bdk/module/rustbdk/bdk-sys/lib/libbdkffi_"
        + interpreter["platform"]
        + ".a",
    ).split()[0]
    if archive_digest != interpreter["native_archive_sha256"]:
        raise ValueError("native archive digest disagrees with running node")
    return {
        "source_commit": commit,
        "source_tree": tree,
        "build_inputs": BUILD_INPUTS,
        "source_archive_sha256": sha256(archive.read_bytes()).hexdigest(),
        "cargo_lock_sha256": sha256(
            subprocess.check_output(["git", "show", commit + ":Cargo.lock"], cwd=ROOT)
        ).hexdigest(),
        "cargo_command": "cargo build --locked --release",
        "rustc_verbose": rust,
        "cargo": cargo,
        "platform": platform,
        "image_id": release_image,
        "release": release,
        "interpreter": interpreter,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "revision", help="Scary Node Git revision with the operator release fingerprint"
    )
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        receipt = run(args.revision, Path(tmp))
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
