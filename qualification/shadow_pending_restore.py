"""Compare eligible payments in two atomic snapshots of the same passive mainnet tip."""

import argparse
import hashlib
import json
import struct
from pathlib import Path

from shadow_native import dsha, inputs, require, unpack_state


def snapshot_inventory(path):
    with path.open("rb") as stream:
        state = unpack_state(stream)
    coins = hashlib.sha256()
    for (txid, index), coin in sorted(state["coins"].items()):
        script = coin["script"]
        coins.update(bytes(txid))
        coins.update(
            struct.pack(
                "<IQI??Q",
                index,
                coin["value"],
                coin["height"],
                coin["coinbase"],
                coin["confiscation"],
                len(script),
            )
        )
        coins.update(script)
    ids = set()
    for txid, entry in state["pending"].items():
        ident = bytes(txid)
        require(
            dsha(entry["raw"]) == ident, "pending transaction body differs from its ID"
        )
        ids.add(ident)
    headers = state["headers"]
    boundary = {
        "height": state["height"],
        "parent": dsha(headers[-1])[::-1].hex(),
        "headers": len(headers),
        "headers_sha256": hashlib.sha256(b"".join(headers)).hexdigest(),
        "witnessed_outputs": len(state["coins"]),
        "witnessed_outputs_sha256": coins.hexdigest(),
        "directive_sequence": state["applied_sequence"],
    }
    waiting = []
    seen = set(ids)
    for source, raw in state.get("waiting", ()):
        require(source in ("sv", "subtree"), "unknown waiting offer transport")
        inputs(raw)
        ident = dsha(raw)
        require(ident not in seen, "duplicate payment in checkpoint offer pools")
        seen.add(ident)
        waiting.append((source, ident, len(raw)))
    return boundary, state["_checkpoint_sha256"], ids, waiting


def inventory(path):
    boundary, digest, ids, _ = snapshot_inventory(path)
    return boundary, digest, ids


def waiting_summary(offers):
    commitment = hashlib.sha256()
    sources = {"sv": 0, "subtree": 0}
    total = 0
    for source, ident, size in offers:
        sources[source] += 1
        total += size
        commitment.update(bytes([0 if source == "sv" else 1]))
        commitment.update(struct.pack("<Q", size))
        commitment.update(ident)
    return {
        "count": len(offers),
        "bytes": total,
        "sources": sources,
        "ordered_sha256": commitment.hexdigest(),
    }


def compare(earlier, closed, *, awaiting_successor=False):
    boundary, first_checksum, first, initial_waiting = snapshot_inventory(earlier)
    last_boundary, last_checksum, last, final_waiting = snapshot_inventory(closed)
    require(
        last_boundary == boundary,
        "checkpoints cover different witnessed branches or directives",
    )
    require(
        first <= last and (awaiting_successor or first < last),
        "controlled stop did not add eligible payments while preserving the original set",
    )
    waiting = {}
    if awaiting_successor:
        require(
            final_waiting[: len(initial_waiting)] == initial_waiting
            and len(final_waiting) > len(initial_waiting),
            "no new waiting offers or the earlier ordered offers changed",
        )
        waiting = {
            "earlier_waiting": waiting_summary(initial_waiting),
            "closed_waiting": waiting_summary(final_waiting),
        }
    return {
        **waiting,
        **boundary,
        "earlier_payload_sha256": first_checksum,
        "closed_payload_sha256": last_checksum,
        "earlier_eligible": len(first),
        "closed_eligible": len(last),
        "added_eligible": len(last - first),
        "removed_eligible": len(first - last),
        "earlier_ids_sha256": hashlib.sha256(b"".join(sorted(first))).hexdigest(),
        "closed_ids_sha256": hashlib.sha256(b"".join(sorted(last))).hexdigest(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("earlier", type=Path)
    parser.add_argument("closed", type=Path)
    parser.add_argument(
        "--awaiting-successor",
        action="store_true",
        help="require new ordered native offers whose funding awaits a successor",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            compare(
                args.earlier, args.closed, awaiting_successor=args.awaiting_successor
            ),
            indent=2,
        )
    )
