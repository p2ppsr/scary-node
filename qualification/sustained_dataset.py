"""Package and restore the exact public regtest wires for the paired native workload."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile

from verify_sustained_native import FUNDING, ORDERED_SOURCE, read_source

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import decode_block, txid
from verify_receipt import check_header, require

GENESIS = "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"
HISTORY_DIGEST = "3d04e4fa099e49dbe556b9d7d83d06f44264507e54f62a0d9653a3a50833a0b3"
HISTORY = [f"{h:08d}.block" for h in range(1, 484)] + [
    "manifest.json",
    "sqlite-receipt.json",
    "LICENSE",
]
PAYMENTS = [
    "funding.block",
    "frames.bin",
    "ids.bin",
    "offsets.json",
    "manifest.json",
    "LICENSE",
]
DATASETS = {"paired-corpus": HISTORY, "sustained-signed": PAYMENTS}


def matches_public(history, payments):
    manifest = json.loads((history / "manifest.json").read_text())
    require(
        set(manifest)
        == {
            "genesis",
            "funding_height",
            "final_height",
            "sha256_of_ordered_wire_digests",
            "wire_bytes_including_setup",
        },
        "unexpected historical manifest fields",
    )
    receipt = json.loads((history / "sqlite-receipt.json").read_text())
    published = (
        Path(__file__).resolve().parents[1]
        / "docs/evidence/chronicle-mixed-native-large.json"
    )
    require(
        receipt == json.loads(published.read_text()), "historical stock receipt differs"
    )
    require(
        manifest["genesis"] == GENESIS
        and manifest["final_height"] == 483
        and manifest["funding_height"] == 451,
        "historical registration differs",
    )
    parent = GENESIS
    ordered = hashlib.sha256()
    wire_bytes = 0
    for h in range(1, 484):
        body = (history / f"{h:08d}.block").read_bytes()
        digest = txid(body[:80])
        check_header(body[:80], digest, parent)
        decode_block(body, digest)
        ordered.update(hashlib.sha256(body).digest())
        parent = digest
        wire_bytes += len(body)
    require(
        ordered.hexdigest()
        == manifest["sha256_of_ordered_wire_digests"]
        == HISTORY_DIGEST
        and wire_bytes == manifest["wire_bytes_including_setup"],
        "historical wire commitments differ",
    )
    block = (payments / "funding.block").read_bytes()
    check_header(block[:80], FUNDING, parent)
    _, txs, outs = decode_block(block, FUNDING)
    require(
        len(txs) == 16385 and sum(len(o) for o in outs[1:]) == 65536,
        "funding split differs",
    )
    manifest = json.loads((payments / "manifest.json").read_text())
    require(
        set(manifest)
        == {
            "scheduled",
            "rails",
            "batch_size",
            "batches",
            "offered_id_sha256",
            "predecessor_height",
            "predecessor_hash",
            "native_wire_bytes",
        },
        "unexpected payment manifest fields",
    )
    require(
        manifest["offered_id_sha256"] == ORDERED_SOURCE
        and manifest["predecessor_height"] == 483
        and manifest["predecessor_hash"] == parent,
        "registered signed funding differs",
    )
    entries = len(read_source(payments, manifest))
    return {
        "linked_regtest_history_blocks": 483,
        "history_wire_bytes": wire_bytes,
        "history_digest": ordered.hexdigest(),
        "ordinary_funding_outputs": 65536,
        "funding_block": FUNDING,
        "signed_native_envelopes": entries,
        "ordered_internal_txids_sha256": ORDERED_SOURCE,
    }


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def pack(history, payments, output):
    output.mkdir(parents=True, exist_ok=True)
    measured = matches_public(history, payments)
    assets = []
    for root, source in (("paired-corpus", history), ("sustained-signed", payments)):
        path = output / (root + ".tar.gz")
        temp = path.with_suffix(".partial")
        with (
            temp.open("wb") as f,
            gzip.GzipFile(
                filename="", mode="wb", fileobj=f, mtime=0, compresslevel=1
            ) as gz,
            tarfile.open(fileobj=gz, mode="w|", format=tarfile.USTAR_FORMAT) as tar,
        ):
            for name in DATASETS[root]:
                p = (
                    Path(__file__).resolve().parents[1] / "LICENSE"
                    if name == "LICENSE"
                    else source / name
                )
                member = tarfile.TarInfo(root + "/" + name)
                member.size = p.stat().st_size
                member.mode = 0o644
                with p.open("rb") as contents:
                    tar.addfile(member, contents)
        temp.replace(path)
        assets.append(
            {
                "filename": path.name,
                "bytes": path.stat().st_size,
                "sha256": digest(path),
                "regular_members": len(DATASETS[root]),
            }
        )
    return {"network": "regtest", "assets": assets, "content": measured}


def unpack(assets, destination, receipt):
    paths = {r["filename"]: r for r in receipt["assets"]}
    require(set(paths) == {r + ".tar.gz" for r in DATASETS}, "dataset assets differ")
    require(
        not destination.exists(),
        "choose a new destination for the two data directories",
    )
    destination.mkdir(parents=True)
    try:
        for root, members in DATASETS.items():
            p = assets / (root + ".tar.gz")
            require(
                p.stat().st_size == paths[p.name]["bytes"]
                and digest(p) == paths[p.name]["sha256"],
                "asset digest differs",
            )
            directory = destination / root
            directory.mkdir()
            expected = {root + "/" + m for m in members}
            with tarfile.open(p, mode="r|gz") as tar:
                for item in tar:
                    require(
                        item.isreg() and item.name in expected,
                        "unexpected or duplicated archive entry",
                    )
                    expected.remove(item.name)
                    with (
                        tar.extractfile(item) as inp,
                        (directory / Path(item.name).name).open("xb") as out,
                    ):
                        shutil.copyfileobj(inp, out, length=1 << 20)
            require(not expected, "archive missing required entries")
        measured = matches_public(
            destination / "paired-corpus", destination / "sustained-signed"
        )
        require(measured == receipt["content"], "reconstructed content differs")
        return measured
    except Exception:
        # The directory was newly created exclusively for this extraction.
        shutil.rmtree(destination)
        raise


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="mode", required=True)
    create = sub.add_parser("pack")
    create.add_argument("history", type=Path)
    create.add_argument("payments", type=Path)
    create.add_argument("output", type=Path)
    restore = sub.add_parser("unpack")
    restore.add_argument("assets", type=Path)
    restore.add_argument("destination", type=Path)
    restore.add_argument(
        "--receipt",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "docs/evidence/chronicle-sustained-source-assets.json",
    )
    a = p.parse_args()
    if a.mode == "pack":
        result = pack(a.history, a.payments, a.output)
        (a.output / "source-assets.json").write_text(
            json.dumps(result, indent=2) + "\n"
        )
    else:
        result = unpack(a.assets, a.destination, json.loads(a.receipt.read_text()))
    print(json.dumps(result, indent=2))
