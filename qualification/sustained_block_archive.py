"""Reconstruct exact observed regtest block bytes from the shared signed wallet source."""

import argparse
import array
import gzip
import hashlib
import json
import mmap
import os
import struct
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

from live_cadence_body import source_frames
from sustained_outcomes import HASH, digest
from verify_peer_intake import parse_tx, read_varint
from verify_sustained_native import (
    ORDERED_SOURCE,
    PAYMENTS,
    decode_block,
    dsha,
    observed_bodies,
    read_source,
    require,
    verify,
)


@contextmanager
def signed_frames(corpus, source):
    require(
        source == json.loads((corpus / "manifest.json").read_bytes())
        and source["offered_id_sha256"] == digest(corpus / "ids.bin") == ORDERED_SOURCE
        and source["scheduled"] == PAYMENTS,
        "different signed source",
    )
    with (
        (corpus / "frames.bin").open("rb") as f,
        mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as wire,
    ):
        require(len(wire) == source["native_wire_bytes"], "source frame length differs")
        start = 0
        positions = array.array("Q")
        for transaction, _ in source_frames(corpus, PAYMENTS):
            positions.append(start)
            start += 24 + len(transaction)
        require(start == len(wire), "extra signed frames")
        positions.append(start)
        yield wire, positions


def assemble(prefix, members, frames, positions, expected_bytes):
    count, pos = read_varint(prefix, 80)
    _, _, end = parse_tx(prefix, pos)
    require(
        end == len(prefix) and len(members) == 4 * (count - 1),
        "invalid coinbase or source-ordinal count",
    )
    raw = bytearray(prefix)
    for (ordinal,) in struct.iter_unpack("<I", members):
        require(ordinal < len(positions) - 1, "source ordinal out of range")
        start = positions[ordinal] + 24
        stop = positions[ordinal + 1]
        require(
            len(raw) + stop - start <= expected_bytes,
            "reconstructed body exceeds its observed size",
        )
        raw.extend(frames[start:stop])
    return raw


def checked(wire, row):
    ident, digest_value = row["hash"], row["wire_sha256"]
    require(
        len(wire) == row["wire_bytes"]
        and wire[:80].hex() == row["header_hex"]
        and dsha(wire[:80])[::-1].hex() == ident
        and hashlib.sha256(wire).hexdigest() == digest_value
        and row["independent_wire_sha256"]
        == dict.fromkeys(("sv", "tn1", "tn2"), digest_value),
        "body differs from the original independent stock reads",
    )


def read_receipt(receipt):
    with gzip.open(receipt, "rt") as f:
        record = json.load(f)
    verify(record)
    rows = observed_bodies(record)
    require(
        rows and all(HASH.fullmatch(h) for h in rows),
        "invalid observed block identifiers",
    )
    return record["source"], rows


def put(z, name, data):
    info = zipfile.ZipInfo(name)
    z.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=3)


def binding(receipt, frames):
    return {
        "version": 1,
        "study_receipt_sha256": digest(receipt),
        "ordered_txids_sha256": ORDERED_SOURCE,
        "frames_sha256": hashlib.sha256(frames).hexdigest(),
    }


def pack(receipt, corpus, block_dir, output):
    source, rows = read_receipt(receipt)
    ordinals = None
    with (
        tempfile.TemporaryDirectory(dir=output.parent) as temp,
        signed_frames(corpus, source) as (frames, positions),
    ):
        tmp = Path(temp) / "blocks.zip"
        with zipfile.ZipFile(tmp, "x") as archive:
            put(
                archive,
                "source.json",
                json.dumps(binding(receipt, frames), sort_keys=True).encode(),
            )
            for ident, row in rows.items():
                original = (block_dir / (ident + ".block")).read_bytes()
                checked(original, row)
                count, pos = read_varint(original, 80)
                _, _, end = parse_tx(original, pos)
                index = block_dir / (ident + ".index")
                if index.exists() and index.stat().st_size == (count - 1) * 4:
                    members = index.read_bytes()
                else:
                    # The public full-wire download has no local intake index.
                    if ordinals is None:
                        ordinals = read_source(corpus, source)
                    _, txs, _ = decode_block(original, ident)
                    sequence = [ordinals.get(dsha(tx)) for tx in txs[1:]]
                    if None in sequence:
                        put(archive, ident + ".block", original)
                        continue
                    members = b"".join(struct.pack("<I", i) for i in sequence)
                prefix = original[:end]
                require(
                    assemble(prefix, members, frames, positions, len(original))
                    == original,
                    "available source ordinals do not reproduce the original body",
                )
                put(archive, ident + ".prefix", prefix)
                put(archive, ident + ".ordinals", members)
        summary = replay(receipt, corpus, tmp)
        os.link(tmp, output)
        return summary


def reconstruct(receipt, corpus, archive_path):
    """Yield each unique active or detached (block record, verified wire) from the closed study."""
    source, rows = read_receipt(receipt)
    with (
        signed_frames(corpus, source) as (frames, positions),
        zipfile.ZipFile(archive_path) as archive,
    ):
        names = set(archive.namelist())
        require(
            len(names) == len(archive.infolist())
            and archive.getinfo("source.json").file_size < 1024,
            "invalid archive members",
        )
        require(
            json.loads(archive.read("source.json")) == binding(receipt, frames),
            "source or study changed",
        )
        names.remove("source.json")
        for ident, row in rows.items():
            size = row["wire_bytes"]
            literal = ident + ".block"
            if literal in names:
                require(
                    archive.getinfo(literal).file_size == size,
                    "literal body length differs",
                )
                wire = archive.read(literal)
                names.remove(literal)
            else:
                prefix, members = ident + ".prefix", ident + ".ordinals"
                require(
                    archive.getinfo(prefix).file_size <= size
                    and archive.getinfo(members).file_size
                    == 4 * row["ordinary_transactions"],
                    "incorrect source index dimensions",
                )
                wire = assemble(
                    archive.read(prefix), archive.read(members), frames, positions, size
                )
                names.difference_update((prefix, members))
            checked(wire, row)
            yield row, wire
        require(not names, "unaccounted archive member")


def replay(receipt, corpus, archive, directory=None):
    count = total = 0
    for row, raw in reconstruct(receipt, corpus, archive):
        if directory is not None:
            with (directory / (row["hash"] + ".block")).open("xb") as f:
                f.write(raw)
        count += 1
        total += len(raw)
    return {
        "unique_blocks": count,
        "verified_wire_bytes": total,
        "archive_bytes": archive.stat().st_size,
        "archive_sha256": digest(archive),
    }


def unpack(receipt, corpus, archive, output):
    require(not output.exists(), "choose a fresh directory for reconstructed blocks")
    with tempfile.TemporaryDirectory(dir=output.parent) as base:
        temporary = Path(base) / "blocks"
        temporary.mkdir()
        summary = replay(receipt, corpus, archive, temporary)
        temporary.rename(output)
        return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest="action", required=True)
    for name in ("pack", "verify", "unpack"):
        sub = s.add_parser(name)
        sub.add_argument("receipt", type=Path)
        sub.add_argument("corpus", type=Path)
        sub.add_argument("archive", type=Path)
        if name != "verify":
            sub.add_argument("blocks", type=Path)
    a = p.parse_args()
    if a.action == "pack":
        summary = pack(a.receipt, a.corpus, a.blocks, a.archive)
    elif a.action == "unpack":
        summary = unpack(a.receipt, a.corpus, a.archive, a.blocks)
    else:
        summary = replay(a.receipt, a.corpus, a.archive)
    print(json.dumps(summary, indent=2))
