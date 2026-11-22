"""Read confirmed local block bodies and compare their published stock Teranode metadata."""

import argparse
import gzip
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import decode_block, txid


def collect(journal, directory, base):
    authority = urlsplit(base)
    if (
        authority.scheme != "https"
        or not authority.hostname
        or authority.username
        or authority.password
        or authority.query
        or authority.fragment
    ):
        raise ValueError("supply a public HTTPS Asset base")
    tips = {}
    with journal.open("rb") as source:
        for line in source:
            row = json.loads(line)
            if row.get("height") in range(976547, 976556) and row.get("header_hex"):
                tips[row["height"]] = row
    observations = {}
    for height in range(976547, 976556):
        tip = tips[height]
        h = tip["hash"]
        # The only remote query is for a previously selected block. Asset metadata
        # supplies the native partition, which is absent from a Bitcoin wire block.
        with urlopen(base.rstrip("/") + "/block/" + h + "/json", timeout=12) as res:
            encoded = res.read(65537)
        if len(encoded) > 65536:
            raise ValueError("reference metadata exceeds the bounded capture")
        ref = json.loads(encoded)
        with gzip.open(directory / (h + ".gz"), "rb") as source:
            raw = source.read(256 * 1024 * 1024 + 1)
        if (
            len(raw) > 256 * 1024 * 1024
            or raw[:80].hex() != tip["header_hex"]
            or ref["height"] != height
        ):
            raise ValueError("unexpected reference block")
        _, txs, _ = decode_block(raw, h)
        observations[height] = {
            "height": height,
            "hash": h,
            "header_hex": raw[:80].hex(),
            "transaction_ids": [txid(tx) for tx in txs],
            "reference_subtrees": ref["subtrees"],
            "reference_header": ref["header"],
            "asset_tx_count": ref["transaction_count"],
            "asset_size": ref["size_in_bytes"],
            "wire_bytes": len(raw),
            "metadata_bytes": len(encoded),
            "observed_at": tip["observed_at"],
            "coverage": tip["coverage"],
        }
        time.sleep(1)
    return observations


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("journal", type=Path)
    p.add_argument("body_directory", type=Path)
    p.add_argument(
        "asset_base", help="approved public Teranode Asset base ending in /api/v1"
    )
    p.add_argument("output", type=Path)
    a = p.parse_args()
    rows = collect(a.journal, a.body_directory, a.asset_base)
    with a.output.open("x") as target:
        print(json.dumps(rows, separators=(",", ":")), file=target)
