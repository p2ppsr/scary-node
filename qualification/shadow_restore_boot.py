"""Restore an immutable mainnet observer checkpoint with no incoming network data."""

import argparse
import hashlib
import json
from pathlib import Path

from restore_witness_archive import docker, restored_status
from run_cohort import session_lock
from shadow_pending_restore import inventory


def require(condition, message):
    if not condition:
        raise ValueError(message)


def restore(checkpoint, ledger, build):
    boundary, checksum, payments = inventory(checkpoint)
    release = json.loads(build.read_text())
    image = release["image_id"]
    with checkpoint.open("rb") as source:
        wire_sha = hashlib.file_digest(source, "sha256").hexdigest()
    # Only the signed prefix and receipt files are copied. The restored process
    # generates a fresh identity. Its data, locks and token live in private RAM.
    init = """
        mkdir -p /data/mainnet/alerts
        ln -s /checkpoint /data/mainnet/witness.state
        i=1
        while [ "$i" -le "$1" ]; do
            prefix=$(printf '%010d' "$i")
            cp "/ledger/$prefix.signed" "/ledger/$prefix.receipt" /data/mainnet/alerts/
            i=$((i+1))
        done
        printf 'isolated-offline-restore' > /data/token
        exec scary-node --network mainnet --data-dir /data --sv-peer 127.0.0.1:1 \
            --listen /ip4/127.0.0.1/tcp/9905 --api-bind 127.0.0.1:8080 \
            --alert-service-url http://127.0.0.1:1 \
            --alert-token-file /data/token
    """
    cid = docker(
        "create",
        "--pull=never",
        "--read-only",
        "--network",
        "none",
        "--log-driver",
        "none",
        "--tmpfs",
        "/data:rw",
        "--mount",
        f"type=bind,src={checkpoint.resolve(strict=True)},dst=/checkpoint,readonly",
        "--mount",
        f"type=bind,src={ledger.resolve(strict=True)},dst=/ledger,readonly",
        "--entrypoint",
        "sh",
        image,
        "-eu",
        "-c",
        init,
        "observer-restore",
        str(boundary["directive_sequence"]),
    )
    try:
        docker("start", cid)
        require(
            docker("exec", cid, "sha256sum", "/checkpoint").split()[0] == wire_sha,
            "the private checkpoint changed before restore",
        )
        status = restored_status(cid)
        m = status["mining"]
        checked = {
            "height": m["height"],
            "parent": m["tip_hash"],
            "headers": m["headers"],
            "witnessed_outputs": m["witnessed_outputs"],
            "directive_sequence": m["durable_directive_sequence"],
        }
        require(
            all(boundary[k] == v for k, v in checked.items()),
            "restored boundary differs",
        )
        require(
            status["release"]["executable_sha256"]
            == release["release"]["executable_sha256"]
            and m["interpreter"]["native_archive_sha256"]
            == release["interpreter"]["native_archive_sha256"],
            "running binary or native BDK archive differs from the build",
        )
        require(
            m["connected_data_peers"] == 0
            and not m["tip_confirmed_since_disconnect"]
            and not m["directives"]["connected"]
            and not m["directives"]["ready"]
            and m["directives"]["last_applied"] == boundary["directive_sequence"]
            and m["directives"]["last_verified"] == boundary["directive_sequence"]
            and all(
                n == 0
                for source in m["peer_transaction_intake"].values()
                for n in source.values()
            )
            and not m["job_preconditions_met"]
            and m["storage_failure"] is None,
            "unexpected connectivity, admission, directive, readiness or storage state",
        )
        require(
            m["pending_transactions"] == len(payments),
            "eligible payment loss during restore",
        )
        return {
            "network": "mainnet",
            "docker_network_mode": "none",
            "source_commit": release["source_commit"],
            "image_id": image,
            "executable_sha256": status["release"]["executable_sha256"],
            "native_archive_sha256": m["interpreter"]["native_archive_sha256"],
            "checkpoint_sha256": wire_sha,
            "payload_sha256": checksum,
            **boundary,
            "expected_eligible": len(payments),
            "restored_eligible": m["pending_transactions"],
            "input_eligible_ids_sha256": hashlib.sha256(
                b"".join(sorted(payments))
            ).hexdigest(),
            "connected_data_peers": m["connected_data_peers"],
            "tip_reconfirmed": m["tip_confirmed_since_disconnect"],
            "directive_connected": m["directives"]["connected"],
            "directive_ready": m["directives"]["ready"],
            "peer_intake": m["peer_transaction_intake"],
            "work_ready": m["job_preconditions_met"],
        }
    finally:
        docker("rm", "-f", cid)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("checkpoint", type=Path, help="immutable witness.state file")
    p.add_argument(
        "signed_ledger", type=Path, help="private signed alert journal directory"
    )
    p.add_argument(
        "release_build", type=Path, help="Linux build receipt for an available image"
    )
    p.add_argument("output", type=Path)
    args = p.parse_args()
    if args.output.exists():
        p.error("choose a new output path")
    with session_lock():
        receipt = restore(args.checkpoint, args.signed_ledger, args.release_build)
    with args.output.open("x") as stream:
        print(json.dumps(receipt, indent=2), file=stream)
