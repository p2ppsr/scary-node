"""Retain and verify all selected predecessor and workload blocks for a paired native replay."""

import argparse
import hashlib
import json
import time
from urllib.error import HTTPError
from pathlib import Path

from mixed_native import NODES, block, rpc, txid
from prove import decode_block
from run_cohort import session_lock
from verify_mixed_native_load import check_header, verify

REGTEST_GENESIS = "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"


def path_at(directory, height):
    return directory / f"{height:08}.block"


def inspect(directory, receipt, *, capture=False):
    verify(receipt)
    rows = {row["height"]: row for row in receipt["rows"]}
    funding_height = min(rows) - 1
    previous = REGTEST_GENESIS
    root = hashlib.sha256()
    total = 0
    for height in range(1, max(rows) + 1):
        path = path_at(directory, height)
        if capture and not path.exists():
            selected = rpc(NODES[0], "getblockhash", height)
            for attempt in range(7):
                try:
                    raw = block(NODES[0], selected)[0]
                    break
                except RuntimeError as error:
                    cause = error.__cause__
                    if (
                        not isinstance(cause, HTTPError)
                        or cause.code != 429
                        or attempt == 6
                    ):
                        raise
                    retry = cause.headers.get("Retry-After")
                    time.sleep(
                        float(retry) if retry and retry.isdecimal() else 2**attempt
                    )
            tmp = path.with_suffix(".next")
            tmp.write_bytes(raw)
            tmp.replace(path)
        else:
            raw = path.read_bytes()
            decode_block(raw, txid(raw[:80]))
        h = txid(raw[:80])
        check_header(raw[:80], h, previous)
        previous = h
        digest = hashlib.sha256(raw).digest()
        root.update(digest)
        total += len(raw)
        if height == funding_height and (h, raw[:80].hex()) != (
            receipt["funding_block"],
            receipt["funding_header"],
        ):
            raise ValueError("captured funding boundary differs")
        if height in rows and (h, len(raw), digest.hex()) != (
            rows[height]["hash"],
            rows[height]["bytes"],
            rows[height]["wire_sha256"],
        ):
            raise ValueError(f"captured workload differs at {height}")
    return {
        "genesis": REGTEST_GENESIS,
        "funding_height": funding_height,
        "final_height": max(rows),
        "sha256_of_ordered_wire_digests": root.hexdigest(),
        "wire_bytes_including_setup": total,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("sqlite_receipt", type=Path)
    p.add_argument("directory", type=Path)
    args = p.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    with session_lock():
        receipt = json.loads(args.sqlite_receipt.read_text())
        details = inspect(args.directory, receipt, capture=True)
        (args.directory / "sqlite-receipt.json").write_text(json.dumps(receipt) + "\n")
        (args.directory / "manifest.json").write_text(
            json.dumps(details, indent=2) + "\n"
        )
    print(json.dumps(details, indent=2))
