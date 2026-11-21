"""Generate the registered finite signed workload, its funding split and its scheduled native wire batches."""

import argparse
import hashlib
import json
import multiprocessing
from pathlib import Path
import shutil
import struct
from native_workload import ChronicleHeightTestsCase
from mixed_corpus import path_at
from prove import decode_block, dsha, txid
from run_cohort import session_lock

COUNT = 5529600
RAILS = 65536
BATCH = 512
SHARDS = 8
MAGIC = bytes.fromhex("dab5bffa")
VERSION = struct.pack("<I", 1)
SEQUENCE = bytes.fromhex("ffffffff")
LOCK = bytes(4)
TYPE = struct.pack("<I", 0x41)
SEQHASH = dsha(SEQUENCE)


def owner(i):
    return ChronicleHeightTestsCase.make_key(
        hashlib.sha256(f"eight sustained regtest owners {i}".encode()).digest()
    )


def locking(k):
    pub = k.get_pubkey()
    return bytes([len(pub)]) + pub + b"\xac"


def signed(outpoint, amount, script, key, outputs):
    output_bytes = b"".join(struct.pack("<QB", v, len(s)) + s for v, s in outputs)
    preimage = (
        VERSION
        + dsha(outpoint)
        + SEQHASH
        + outpoint
        + bytes([len(script)])
        + script
        + struct.pack("<Q", amount)
        + SEQUENCE
        + dsha(output_bytes)
        + LOCK
        + TYPE
    )
    signature = key.sign(dsha(preimage)) + b"\x41"
    script_sig = bytes([len(signature)]) + signature
    return (
        VERSION
        + b"\x01"
        + outpoint
        + bytes([len(script_sig)])
        + script_sig
        + SEQUENCE
        + bytes([len(outputs)])
        + output_bytes
        + LOCK
    )


def packet(raw):
    digest = dsha(raw)
    return digest, MAGIC + b"tx" + bytes(10) + struct.pack("<I", len(raw)) + digest[
        :4
    ] + raw


def generate_shard(args):
    shard, block_file, out = args
    old = ChronicleHeightTestsCase.make_key(b"disposable native throughput fixture")
    old_script = locking(old)
    keys = [owner(i) for i in range(8)]
    scripts = list(map(locking, keys))
    raw_block = block_file.read_bytes()
    _, txs, vouts = decode_block(raw_block, txid(raw_block[:80]))
    if len(txs) != 16385:
        raise ValueError("the predecessor needs 16,384 funded outputs")
    per = RAILS // SHARDS
    first = shard * per
    prev = []
    with (out / f"funding-{shard}.bin").open("wb") as funds:
        for i in range(first // 4, (first + per) // 4):
            amount, script = vouts[i + 1][0]
            if script != old_script:
                raise ValueError(
                    "source output does not belong to the prior workload signer"
                )
            deposit = (amount - 500) // 4
            dest = [(deposit, scripts[(i * 4 + n) % 8]) for n in range(4)]
            raw = signed(dsha(txs[i + 1]) + bytes(4), amount, script, old, dest)
            funds.write(struct.pack("<I", len(raw)) + raw)
            h = dsha(raw)
            prev.extend((h + struct.pack("<I", n), deposit) for n in range(4))
    for generation in range((COUNT + RAILS - 1) // RAILS):
        active = min(per, max(0, COUNT - generation * RAILS - first))
        if not active:
            break
        if active % BATCH:
            raise ValueError("incomplete scheduled batch")
        with (
            (out / f"wire-{generation}-{shard}").open("wb") as wire,
            (out / f"ids-{generation}-{shard}").open("wb") as ids,
            (out / f"offsets-{generation}-{shard}").open("w") as index,
        ):
            for i in range(active):
                k = (first + i + generation) % 8
                point, amount = prev[i]
                raw = signed(
                    point,
                    amount,
                    scripts[k],
                    keys[k],
                    [(amount - 200, scripts[(k + 1) % 8])],
                )
                digest, frame = packet(raw)
                wire.write(frame)
                ids.write(digest)
                prev[i] = (digest + bytes(4), amount - 200)
                if (i + 1) % BATCH == 0:
                    index.write(f"{wire.tell()}\n")
    return shard


def generate(corpus, out):
    source = json.loads((corpus / "sqlite-receipt.json").read_text())
    last = source["rows"][-1]
    if len(source["rows"]) != 32 or source["transactions_per_block"] != 16384:
        raise ValueError("registered prior paired workload required")
    out.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(out).free < 24 * 1024**3:
        raise ValueError("insufficient corpus preparation headroom")
    with multiprocessing.get_context("spawn").Pool(6) as pool:
        for shard in pool.imap_unordered(
            generate_shard,
            [(n, path_at(corpus, last["height"]), out) for n in range(SHARDS)],
        ):
            print("Completed signing shard", shard, flush=True)
    offsets = [0]
    with (
        (out / "frames.bin").open("wb") as merged,
        (out / "ids.bin").open("wb") as merged_ids,
    ):
        for generation in range((COUNT + RAILS - 1) // RAILS):
            for shard in range(SHARDS):
                wire = out / f"wire-{generation}-{shard}"
                if not wire.exists():
                    continue
                ids = out / f"ids-{generation}-{shard}"
                index = out / f"offsets-{generation}-{shard}"
                base = merged.tell()
                offsets.extend(base + int(n) for n in index.read_text().splitlines())
                with wire.open("rb") as f:
                    shutil.copyfileobj(f, merged)
                with ids.open("rb") as f:
                    shutil.copyfileobj(f, merged_ids)
                for part in (wire, ids, index):
                    part.unlink()
    if (
        len(offsets) != COUNT // BATCH + 1
        or (out / "ids.bin").stat().st_size != 32 * COUNT
    ):
        raise ValueError("signed source count differs")
    with (out / "ids.bin").open("rb") as f:
        id_hash = hashlib.file_digest(f, "sha256").hexdigest()
    (out / "offsets.json").write_text(json.dumps(offsets))
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "scheduled": COUNT,
                "rails": RAILS,
                "batch_size": BATCH,
                "batches": COUNT // BATCH,
                "offered_id_sha256": id_hash,
                "predecessor_height": last["height"],
                "predecessor_hash": last["hash"],
                "native_wire_bytes": offsets[-1],
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("paired_corpus", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    with session_lock():
        generate(args.paired_corpus, args.output)
