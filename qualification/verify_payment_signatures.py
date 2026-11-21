"""Verify every funded P2PK transfer in the registered offer corpus independently of its inclusion."""

import argparse
import hashlib
import io
import json
import multiprocessing
from pathlib import Path
import struct
import subprocess
import sys

from verify_sustained_native import (
    BATCH_SIZE,
    FUNDING,
    MAGIC,
    ORDERED_SOURCE,
    PAYMENTS,
    TX_COMMAND,
)
from prove import decode_block, dsha
from verify_receipt import check_header, require

ROOT = Path(__file__).resolve().parents[1]
SV = ROOT / ".native/bitcoin-sv"
SV_COMMIT = "6504a3aff65ba97c0f6c80962b033e35ecbfed4b"
sys.path.insert(0, str(SV / "test/functional"))
from test_framework.key import CECKey
from test_framework.mininode import CTransaction
from test_framework.script import SignatureHash

RAILS = 65536
SHARDS = 8


def public_keys(scripts):
    result = {}
    for script in scripts:
        require(
            len(script) in (35, 67)
            and script[0] == len(script) - 2
            and script[-1] == 0xAC,
            "source is not the registered pay-to-public-key form",
        )
        k = CECKey()
        require(k.set_pubkey(script[1:-1]), "invalid source public key")
        result[script] = k
    require(len(result) == 8, "registered output owners differ")
    return result


def signature(raw, prev, next_script, keys):
    transaction = CTransaction()
    data = io.BytesIO(raw)
    transaction.deserialize(data)
    require(
        data.tell() == len(raw)
        and transaction.nVersion == 1
        and transaction.nLockTime == 0
        and len(transaction.vin) == len(transaction.vout) == 1,
        "payment serialization differs",
    )
    point, value, script = prev
    inp = transaction.vin[0]
    require(
        inp.prevout.serialize() == point and inp.nSequence == 0xFFFFFFFF,
        "payment spends the wrong predecessor",
    )
    dest = transaction.vout[0]
    require(
        value >= 200
        and dest.nValue == value - 200
        and bytes(dest.scriptPubKey) == next_script,
        "recipient or fee differs",
    )
    unlocking = bytes(inp.scriptSig)
    require(
        3 <= len(unlocking) <= 75
        and unlocking[0] == len(unlocking) - 1
        and unlocking[-1] == 0x41,
        "signature encoding or hash type differs",
    )
    digest = SignatureHash(script, transaction, 0, unlocking[-1], value)
    require(
        keys[script].verify(digest, unlocking[1:-1]),
        "signature failed against actual predecessor",
    )
    return dest.nValue


def verify_rail_group(args):
    corpus, group, current, scripts, offsets = args
    keys = public_keys(scripts)
    width = RAILS // SHARDS
    first = group * width
    verified = 0
    with (
        (corpus / "frames.bin").open("rb") as wire,
        (corpus / "ids.bin").open("rb") as ids,
    ):
        for generation, start in enumerate(range(first, PAYMENTS, RAILS)):
            count = min(PAYMENTS - start, width)
            wire.seek(offsets[start // BATCH_SIZE])
            ids.seek(32 * start)
            for i in range(count):
                header = wire.read(24)
                require(
                    header[:4] == MAGIC and header[4:16] == TX_COMMAND,
                    "unexpected transaction framing",
                )
                size = struct.unpack_from("<I", header, 16)[0]
                require(0 < size <= 1024, "unexpected registered transaction size")
                raw = wire.read(size)
                h = dsha(raw)
                require(
                    len(raw) == size and header[20:24] == h[:4] and ids.read(32) == h,
                    "native or ordered ID does not match signed bytes",
                )
                dest = scripts[(first + i + generation + 1) % len(scripts)]
                amount = signature(raw, current[i], dest, keys)
                current[i] = (h + bytes(4), amount, dest)
                verified += 1
            require(
                wire.tell() == offsets[(start + count) // BATCH_SIZE],
                "offer batch ended at wrong boundary",
            )
    return {
        "rail_group": group,
        "first_rail": first,
        "funded_rails": width,
        "signatures": verified,
        "remaining_satoshis": sum(p[1] for p in current),
    }


def verify(corpus, workers):
    require(1 <= workers <= SHARDS, "choose between one and eight workers")
    require(
        subprocess.check_output(
            ["git", "-C", str(SV), "rev-parse", "HEAD"], text=True
        ).strip()
        == SV_COMMIT,
        "reference source revision differs",
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(SV),
            "diff",
            "--quiet",
            "HEAD",
            "--",
            "test/functional/test_framework",
        ],
        check=True,
    )
    manifest = json.loads((corpus / "manifest.json").read_text())
    require(
        manifest["offered_id_sha256"] == ORDERED_SOURCE
        and manifest["scheduled"] == PAYMENTS,
        "registered source identity differs",
    )
    with (corpus / "ids.bin").open("rb") as ids:
        require(
            hashlib.file_digest(ids, "sha256").hexdigest() == ORDERED_SOURCE
            and (corpus / "ids.bin").stat().st_size == 32 * PAYMENTS,
            "ordered corpus digest differs",
        )
    funding = (corpus / "funding.block").read_bytes()
    check_header(funding[:80], FUNDING, manifest["predecessor_hash"])
    _, txs, outs = decode_block(funding, FUNDING)
    rails = []
    for tx, outputs in zip(txs[1:], outs[1:], strict=True):
        h = dsha(tx)
        rails.extend(
            (h + struct.pack("<I", i), amount, script)
            for i, (amount, script) in enumerate(outputs)
        )
    require(len(rails) == RAILS, "unexpected funding outputs")
    scripts = [p[2] for p in rails[:8]]
    require(
        all(p[2] == scripts[i % 8] for i, p in enumerate(rails)),
        "funded owner sequence differs",
    )
    offsets = json.loads((corpus / "offsets.json").read_text())
    require(
        len(offsets) == PAYMENTS // BATCH_SIZE + 1
        and offsets[0] == 0
        and offsets[-1] == (corpus / "frames.bin").stat().st_size,
        "offsets differ",
    )
    width = RAILS // SHARDS
    with multiprocessing.get_context("spawn").Pool(workers) as pool:
        results = pool.map(
            verify_rail_group,
            [
                (corpus, i, rails[i * width : (i + 1) * width], scripts, offsets)
                for i in range(SHARDS)
            ],
        )
    count = sum(r["signatures"] for r in results)
    initial = sum(p[1] for p in rails)
    remaining = sum(r["remaining_satoshis"] for r in results)
    require(
        count == PAYMENTS and initial - remaining == count * 200,
        "aggregate signature and fee accounting differs",
    )
    return {
        "network": "regtest",
        "sv_node_source": SV_COMMIT,
        "funding_block": FUNDING,
        "ordered_source_sha256": ORDERED_SOURCE,
        "signed_transfers": count,
        "funded_rails": RAILS,
        "distinct_recipients": len(scripts),
        "sighash_byte": "41",
        "opening_satoshis": initial,
        "final_satoshis": remaining,
        "fees_satoshis": initial - remaining,
        "rail_groups": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    result = verify(args.corpus, args.workers)
    args.receipt.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "rail_groups"}, indent=2))
