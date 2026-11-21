"""Check a published workload result against downloaded native regtest bodies."""

import argparse
import gzip
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

from sustained_block_archive import pack
from sustained_outcomes import HASH, digest
from verify_sustained_native import observed_bodies, verify, verify_wires


def replay(assets, corpus, receipt_path, manifest_path, compact=None):
    manifest = json.loads(manifest_path.read_bytes())
    with gzip.open(receipt_path, "rt") as f:
        receipt = json.load(f)
    summary = verify(receipt)
    if (
        manifest["receipt_sha256"] != digest(receipt_path)
        or manifest["source_ids_sha256"] != receipt["source"]["offered_id_sha256"]
        or manifest["registration"] != receipt["registration"]
    ):
        raise ValueError("outcome manifest is for a different study")

    observed = observed_bodies(receipt)
    if any(not HASH.fullmatch(h) for h in observed):
        raise ValueError("invalid selected block hash")
    blocks = {"blocks/" + b["hash"] + ".block": b for b in observed.values()}
    expected_wallet = {}
    if receipt.get("inventory_final"):
        expected_wallet = {
            name + ".bits": receipt["inventory_final"][name + "_bits_sha256"]
            for name in ("requested", "served")
        }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "blocks").mkdir()
        for item in manifest["assets"]:
            name = item["name"]
            if name != Path(name).name:
                raise ValueError("unexpected asset name")
            asset = assets / name
            if asset.stat().st_size != item["bytes"] or digest(asset) != item["sha256"]:
                raise ValueError(f"downloaded asset differs: {name}")
            if "original_name" in item:
                member = item["original_name"]
                if member not in expected_wallet:
                    raise ValueError("unexpected wallet membership")
                with gzip.open(asset, "rb") as f, (root / member).open("xb") as out:
                    shutil.copyfileobj(f, out)
                if digest(root / member) != expected_wallet.pop(member):
                    raise ValueError("wallet membership digest differs")
                continue
            with tarfile.open(asset, mode="r|gz") as archive:
                for member in archive:
                    block = blocks.pop(member.name, None)
                    if (
                        not block
                        or not member.isreg()
                        or member.size != block["wire_bytes"]
                    ):
                        raise ValueError(
                            "unexpected, repeated or incorrectly sized body"
                        )
                    with (
                        archive.extractfile(member) as f,
                        (root / member.name).open("xb") as out,
                    ):
                        shutil.copyfileobj(f, out)
                    if digest(root / member.name) != block["wire_sha256"]:
                        raise ValueError("body differs from independent peer readback")
        if blocks or expected_wallet:
            raise ValueError("incomplete download")
        measured = verify_wires(receipt, corpus, root / "blocks")
        if (
            measured != manifest["local_wire_verification"]
            or len(receipt["blocks"]) != manifest["selected_blocks"]
            or summary["distinct_confirmed"] != manifest["distinct_payments"]
            or sum(b["wire_bytes"] for b in receipt["blocks"])
            != manifest["total_selected_wire_bytes"]
        ):
            raise ValueError("manifest and independently reconstructed outcome differ")
        result = {"verified": measured, "result": summary}
        if compact is not None:
            result["compact_archive"] = pack(
                receipt_path, corpus, root / "blocks", compact
            )
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assets", type=Path)
    parser.add_argument("corpus", type=Path, help="unpacked signed source directory")
    parser.add_argument("receipt", type=Path, help="published compressed study receipt")
    parser.add_argument("manifest", type=Path, help="published outcome asset manifest")
    parser.add_argument(
        "--compact",
        type=Path,
        help="save a byte-verified source ordinal archive to a fresh path",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            replay(args.assets, args.corpus, args.receipt, args.manifest, args.compact),
            indent=2,
        )
    )
