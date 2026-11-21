"""Compare eligible payments in two atomic snapshots of the same passive mainnet tip."""

import argparse
import hashlib
import json
import struct
from pathlib import Path

from shadow_native import dsha, require, unpack_state


def inventory(path):
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
    return boundary, state["_checkpoint_sha256"], ids


def compare(earlier, closed):
    boundary, first_checksum, first = inventory(earlier)
    last_boundary, last_checksum, last = inventory(closed)
    require(
        last_boundary == boundary,
        "checkpoints cover different witnessed branches or directives",
    )
    require(
        first < last,
        "controlled stop did not add eligible payments while preserving the original set",
    )
    return {
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
    args = parser.parse_args()
    print(json.dumps(compare(args.earlier, args.closed), indent=2))
