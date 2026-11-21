"""Compare recorded unsigned choices with a fixed sample of independently read mainnet successors."""

import argparse
import gzip
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from shadow import check_header, check_template, merkle
from shadow_checkpoint import atomic_write, current_run


SAMPLE = 16
API = "https://api.whatsonchain.com/v1/bsv/main"


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def transaction_ids(values):
    require(isinstance(values, list), "expected an ordered transaction list")
    require(
        all(
            isinstance(v, str) and len(v) == 64 and bytes.fromhex(v).hex() == v
            for v in values
        ),
        "expected canonical transaction IDs",
    )
    return values


def selected(records):
    tips = current_run(records)
    pairs = []
    for parent, child in zip(tips, tips[1:]):
        job = parent.get("unsigned_candidate")
        if not job or not job["transactions"]:
            continue
        require(
            parent["ready"] and parent["pending"] == len(job["transactions"]),
            "incomplete candidate decision",
        )
        check_template(parent)
        check_header(child)
        require(
            child["previous"] == parent["hash"]
            and child["height"] == parent["height"] + 1,
            "successor differs from observed pair",
        )
        pairs.append((parent, child))
        if len(pairs) == SAMPLE:
            break
    return pairs


def request_json(path):
    # Only previously observed block hashes are queried. Template transaction IDs
    # and unconfirmed bytes are never sent to the reference.
    request = Request(
        API + path, headers={"User-Agent": "scary-node-mainnet-qualification/1"}
    )
    time.sleep(2)
    for attempt in range(4):
        try:
            with urlopen(request, timeout=35) as response:
                return json.load(response)
        except HTTPError as e:
            if e.code not in (429, 502, 503, 504) or attempt == 3:
                raise
            delay = e.headers.get("Retry-After")
            if delay is None:
                seconds = 10 * 2**attempt
            elif delay.isdecimal():
                seconds = int(delay)
            else:
                seconds = (
                    parsedate_to_datetime(delay) - datetime.now(UTC)
                ).total_seconds()
            time.sleep(max(1, seconds))


def read_block(child, fetch=request_json):
    block_hash = child["hash"]
    route = "/block/hash/" + block_hash
    block = fetch(route)
    require(
        block["hash"] == block_hash
        and block["height"] == child["height"]
        and block["confirmations"] > 0,
        "reference did not select the observed successor",
    )
    txs = transaction_ids(block["tx"])
    if len(txs) != block["txcount"]:
        parts = block["pages"]["uri"]
        expected = [route + "/page/" + str(i) for i in range(1, len(parts) + 1)]
        require(parts == expected and parts, "incomplete or unexpected block pages")
        paged = []
        for page in expected:
            paged.extend(transaction_ids(fetch(page)))
        if len(txs) + len(paged) == block["txcount"]:
            txs = txs + paged
        else:
            require(
                paged[: len(txs)] == txs,
                "paged transactions disagree with block prefix",
            )
            txs = paged
    require(
        len(txs) == block["txcount"] and txs, "incomplete successor transaction list"
    )
    return {
        "height": child["height"],
        "hash": block_hash,
        "read_at_utc": datetime.now(UTC).isoformat(),
        "confirmed_at_read": block["confirmations"],
        "transactions": txs,
    }


def compare(snapshot, rows):
    pairs = selected(snapshot["records"])
    require(len(pairs) == len(rows), "sample differs from recorded prospective choices")
    results = []
    for (parent, child), row in zip(pairs, rows, strict=True):
        require(
            (row["height"], row["hash"]) == (child["height"], child["hash"])
            and row["confirmed_at_read"] > 0,
            "independent readback belongs to a different successor",
        )
        txs = transaction_ids(row["transactions"])
        require(
            txs and len(txs) == len(set(txs)), "empty or duplicate transaction list"
        )
        root = merkle([bytes.fromhex(v)[::-1] for v in txs])
        require(
            root == bytes.fromhex(child["header_hex"])[36:68],
            "readback Merkle commitment differs",
        )
        choices = set(transaction_ids(parent["unsigned_candidate"]["transactions"]))
        require(len(choices) == parent["pending"], "duplicate candidate choices")
        overlap = choices.intersection(txs[1:])
        results.append(
            {
                "parent_height": parent["height"],
                "parent_hash": parent["hash"],
                "successor_hash": row["hash"],
                "candidate_payments": len(choices),
                "successor_payments": len(txs) - 1,
                "shared_payments": len(overlap),
            }
        )
    return {
        "scheduled_pairs": SAMPLE,
        "measured_pairs": len(rows),
        "sample_complete": len(rows) == SAMPLE,
        "candidate_transaction_opportunities": sum(
            r["candidate_payments"] for r in results
        ),
        "also_in_immediate_successor": sum(r["shared_payments"] for r in results),
        "by_parent": results,
    }


def packed(data):
    return gzip.compress(
        json.dumps(data, separators=(",", ":"), sort_keys=True).encode(), mtime=0
    )


def run(mode, snapshot_path, artifact):
    raw = snapshot_path.read_bytes()
    snapshot = json.loads(gzip.decompress(raw))
    checksum = hashlib.sha256(raw).hexdigest()
    if mode == "capture":
        rows = [read_block(child) for _, child in selected(snapshot["records"])]
        summary = compare(snapshot, rows)
        atomic_write(
            artifact, packed({"snapshot_sha256": checksum, "successors": rows})
        )
    else:
        evidence = json.loads(gzip.decompress(artifact.read_bytes()))
        require(
            evidence["snapshot_sha256"] == checksum,
            "evidence belongs to a different checkpoint",
        )
        summary = compare(snapshot, evidence["successors"])
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("capture", "verify"))
    p.add_argument("snapshot", type=Path)
    p.add_argument("artifact", type=Path)
    args = p.parse_args()
    print(json.dumps(run(args.mode, args.snapshot, args.artifact), indent=2))
