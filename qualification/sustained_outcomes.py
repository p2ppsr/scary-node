"""Package only verified selected regtest bodies and inventory memberships for independent replay."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile
import tempfile

from verify_sustained_native import REGISTRATION, observed_bodies, verify, verify_wires

PART_BYTES = 128 * 1024 * 1024
HASH = re.compile(r"[0-9a-f]{64}")


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def pack(directory, out, corpus):
    if out.exists():
        raise ValueError("outcome asset directory already exists")
    out.parent.mkdir(exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        staging = Path(tmp) / "assets"
        manifest = pack_assets(directory, staging, corpus)
        staging.rename(out)
    return manifest


def pack_assets(directory, out, corpus):
    with gzip.open(directory / "receipt.json.gz", "rt") as f:
        result = json.load(f)
    summary = verify(result)
    local_wires = verify_wires(result, corpus, directory / "blocks")
    out.mkdir(parents=True, exist_ok=False)
    label = result.get(
        "arm", "direct" if result["registration"] == REGISTRATION else "inventory"
    )
    prefix = "native-sustained-" + label
    parts = []
    group = []
    size = 0
    for block in observed_bodies(result).values():
        if not HASH.fullmatch(block["hash"]) or not HASH.fullmatch(
            block["wire_sha256"]
        ):
            raise ValueError("invalid selected body identity")
        if group and size + block["wire_bytes"] > PART_BYTES:
            parts.append(group)
            group = []
            size = 0
        group.append(block)
        size += block["wire_bytes"]
    if group:
        parts.append(group)
    assets = []

    def record(path, **fields):
        assets.append(
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": digest(path),
                **fields,
            }
        )

    for i, group in enumerate(parts, 1):
        path = out / f"{prefix}-bodies-{i:03d}.tar.gz"
        with (
            path.open("xb") as dst,
            gzip.GzipFile(
                filename="", fileobj=dst, mode="wb", compresslevel=1, mtime=0
            ) as compressed,
            tarfile.open(
                fileobj=compressed, mode="w|", format=tarfile.USTAR_FORMAT
            ) as archive,
        ):
            for block in group:
                member = "blocks/" + block["hash"] + ".block"
                raw = directory / member
                if (
                    raw.stat().st_size != block["wire_bytes"]
                    or digest(raw) != block["wire_sha256"]
                ):
                    raise ValueError(
                        "stored block differs from its three original independent reads"
                    )
                info = tarfile.TarInfo(member)
                info.size = block["wire_bytes"]
                info.mode = 0o644
                with raw.open("rb") as f:
                    archive.addfile(info, f)
        record(
            path,
            first_height=group[0]["height"],
            last_height=group[-1]["height"],
            blocks=len(group),
        )
    if result.get("inventory_final"):
        for name in ("requested", "served"):
            original = directory / (name + ".bits")
            expected = result["inventory_final"][name + "_bits_sha256"]
            if digest(original) != expected:
                raise ValueError("wallet membership differs from original commitment")
            path = out / f"{prefix}-{name}.bits.gz"
            with (
                original.open("rb") as f,
                path.open("xb") as dst,
                gzip.GzipFile(
                    filename="", fileobj=dst, mode="wb", compresslevel=6, mtime=0
                ) as gz,
            ):
                shutil.copyfileobj(f, gz)
            record(path, original_name=name + ".bits", uncompressed_sha256=expected)
    manifest = {
        "registration": result["registration"],
        "receipt_sha256": digest(directory / "receipt.json.gz"),
        "source_ids_sha256": result["source"]["offered_id_sha256"],
        "local_wire_verification": local_wires,
        "selected_blocks": len(result["blocks"]),
        "distinct_payments": summary["distinct_confirmed"],
        "total_selected_wire_bytes": sum(b["wire_bytes"] for b in result["blocks"]),
        "assets": assets,
    }
    (out / f"{prefix}-outcomes.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("assets", type=Path)
    parser.add_argument(
        "--corpus",
        type=Path,
        required=True,
        help="original signed ID/envelope artifacts",
    )
    args = parser.parse_args()
    print(json.dumps(pack(args.directory, args.assets, args.corpus), indent=2))
